"""MARKET-ENTRY SWEEP -> DISPLACEMENT CONTINUATION (15m, MGC).

ENTRY = THE DECISION BAR'S CLOSE (honest fill).

Findings #9126/#9133/#9139/#9146 established that the house backtester
FILLS AT THE PRICE THE STRATEGY NAMES.  Every legacy family names a
STRUCTURAL price (FVG midpoint, OB midpoint, the sweep extreme, the 40%
retrace) that belongs to an already-closed bar, so the move from the named
price to the decision price is never risked -- the stamp's PF is a
retro-fill artefact that the broker cannot reproduce.  Measured on the four
pending candidates: stamped PF 1.76-2.38 collapses to 0.56-1.00 under the
production (market at next bar open) fill and 0.70-0.90 under a charitable
limit fill.

This strategy therefore has NO structural entry price: the decision is made
on a closed bar and the entry is that bar's close, which is the price the
broker can pay.  Stops and targets stay structural (that is where the edge
information lives); only the ENTRY is honest.

SIGNAL (15m):
  1. SWEEP -- within the last `confirm_bars` bars a bar took out a rolling
     `lookback` high/low by >= `min_sweep_atr` * ATR and closed back inside
     (a liquidity grab / stop run).
  2. DISPLACEMENT -- the decision bar closes back through the swept level
     with body ratio >= `body_ratio_min` and range >= `disp_k` * ATR.
  3. ENTRY at the decision close; stop beyond the sweep extreme + buffer;
     target at `rr` * risk.

DATA HYGIENE: the deep archive carries placeholder bars (zero range, zero
volume); the strategy refuses to trade them, so the stamp and the live fire
see the same bars.

Stateless; numpy only (sandbox-safe).
"""
import numpy as np

# ---------------------------------------------------------------- defaults ---
# The house engine calls evaluate(bars, {}) -- NO params argument -- and the
# live path merges the ACTIVE ROW's params_json over these defaults
# (portfolio_runtime.py:535), so the two paths agree only if the promoted
# configuration is written HERE as well as in the candidate row.
DEFAULTS = {
    'setup': 'sweep_cont',
    'lookback': 8,
    'min_sweep_atr': 0.45,
    'disp_k': 1.0,
    'rr': 1.5,
    'confirm_bars': 6,
    'body_ratio_min': 0.55,
    'stop_buf_atr': 0.15,
    'atr_period': 14,
    'pullback_bars': 8,
    'retrace': 0.5,
    'stop_atr': 0.8,
    'fvg_lookback': 12,
    'ema_period': 0,
    'target_mode': 'rr',
}


def _atr(H, L, C, period):
    """Simple mean of the last `period` true ranges (numpy, no pandas)."""
    n = len(C)
    if n < 3:
        return float("nan")
    prev_c = C[:-1]
    tr = np.maximum(H[1:] - L[1:],
                    np.maximum(np.abs(H[1:] - prev_c), np.abs(L[1:] - prev_c)))
    p = int(min(period, len(tr)))
    return float(np.mean(tr[-p:]))


def _ema(v, period):
    """Wilder-free simple EMA via a numpy recurrence (fast enough)."""
    alpha = 2.0 / (float(period) + 1.0)
    out = float(v[0])
    for x in v[1:]:
        out += alpha * (float(x) - out)
    return out


def _hours(bars):
    ts = bars["ts"].values
    return (ts.astype("datetime64[h]").astype("int64") % 24).astype(float)


def evaluate(bars, params):
    p = dict(DEFAULTS)
    try:
        p.update({k: v for k, v in (params or {}).items()
                  if k in DEFAULTS and v is not None})
    except Exception:  # noqa: BLE001
        pass
    setup = str(p["setup"])
    n = len(bars)
    if n < 60:
        return None
    cur = n - 1

    O = bars["open"].to_numpy(float)
    H = bars["high"].to_numpy(float)
    L = bars["low"].to_numpy(float)
    C = bars["close"].to_numpy(float)

    # --- DATA HYGIENE ---------------------------------------------------
    # The deep archive carries PLACEHOLDER bars (zero range, zero volume).
    # A strategy that fires on them is trading on stale prices, and the
    # "next bar open" fill would be a stale open.  Refuse to trade them.
    if not (H[cur] > L[cur]):
        return None
    try:
        if "volume" in bars.columns and float(bars["volume"].iloc[cur]) <= 0:
            return None
    except Exception:  # noqa: BLE001
        pass

    a = _atr(H, L, C, int(p.get("atr_period", 14)))
    if not np.isfinite(a) or a <= 0:
        return None

    ocur, hcur, lcur, ccur = O[cur], H[cur], L[cur], C[cur]
    rng = max(hcur - lcur, 1e-9)
    br = abs(ccur - ocur) / rng

    rr = float(p.get("rr", 2.0))
    buf = float(p.get("stop_buf_atr", 0.15))
    min_body = float(p.get("body_ratio_min", 0.55))
    disp_k = float(p.get("disp_k", 0.9))
    stop_atr = float(p.get("stop_atr", 0.8))

    # optional UTC session gate
    s_utc = p.get("session_start_utc")
    if s_utc is not None:
        hh = float(_hours(bars)[cur])
        e_utc = float(p.get("session_end_utc", 24))
        if not (float(s_utc) <= hh < e_utc):
            return None

    # optional trend filter (long above / short below the EMA)
    ema_p = int(p.get("ema_period", 0))
    ema = _ema(C, ema_p) if ema_p > 0 else None

    direction = None
    stop = None
    target_level = None

    # ------------------------------------------------------------------ setups
    if setup in ("sweep_rev", "sweep_cont"):
        lb = int(p.get("lookback", 20))
        ms = float(p.get("min_sweep_atr", 0.20)) * a
        need = lb + 2

        if setup == "sweep_rev":
            if n < need:
                return None
            ph = float(np.max(H[cur - lb:cur]))
            pl = float(np.min(L[cur - lb:cur]))
            if hcur > ph + ms and ccur < ph:
                direction, stop = "short", hcur + buf * a
                target_level = ph
            elif lcur < pl - ms and ccur > pl:
                direction, stop = "long", lcur - buf * a
                target_level = pl
        else:
            confirm = int(p.get("confirm_bars", 6))
            j = -1
            for k in range(cur - 1, max(0, cur - confirm - 1), -1):
                if k < lb + 1:
                    continue
                phk = float(np.max(H[k - lb:k]))
                plk = float(np.min(L[k - lb:k]))
                if L[k] < plk - ms and C[k] > plk:
                    j = k
                    break
                if H[k] > phk + ms and C[k] < phk:
                    j = k
                    break
            if j < 0:
                return None
            plj = float(np.min(L[j - lb:j]))
            phj = float(np.max(H[j - lb:j]))
            if L[j] < plj - ms and C[j] > plj:
                if not (ccur > plj and br >= min_body and rng >= disp_k * a):
                    return None
                direction, stop = "long", float(L[j]) - buf * a
            else:
                if not (ccur < phj and br >= min_body and rng >= disp_k * a):
                    return None
                direction, stop = "short", float(H[j]) + buf * a

    elif setup == "disp_break":
        lb = int(p.get("lookback", 20))
        if n < lb + 2:
            return None
        ph = float(np.max(H[cur - lb:cur]))
        pl = float(np.min(L[cur - lb:cur]))
        if ccur > ph and br >= min_body and rng >= disp_k * a:
            direction, stop = "long", ccur - stop_atr * a
        elif ccur < pl and br >= min_body and rng >= disp_k * a:
            direction, stop = "short", ccur + stop_atr * a

    elif setup == "fvg_touch":
        fl = int(p.get("fvg_lookback", 12))
        for j in range(cur, max(2, cur - fl), -1):
            hj2, hj = float(H[j - 2]), float(H[j])
            lj, lj2 = float(L[j]), float(L[j - 2])
            if lj > hj2:  # bullish FVG (gap up) -> fade the gap down
                zlo, zhi = hj2, lj
                if np.all(L[j + 1:cur] > zhi) and lcur <= zhi:
                    direction, stop = "short", zhi + buf * a
                    target_level = zlo
                    break
            elif hj < lj2:  # bearish FVG (gap down) -> fade the gap up
                zlo, zhi = hj, lj2
                if np.all(H[j + 1:cur] < zlo) and hcur >= zlo:
                    direction, stop = "long", zlo - buf * a
                    target_level = zhi
                    break

    elif setup == "disp_pullback":
        pb = int(p.get("pullback_bars", 5))
        retrace = float(p.get("retrace", 0.5))
        for j in range(cur - 1, max(0, cur - 1 - pb), -1):
            rng_j = float(H[j] - L[j])
            if rng_j <= 0:
                continue
            body_j = abs(float(C[j] - O[j]))
            if body_j / rng_j < min_body or rng_j < disp_k * a:
                continue
            if C[j] <= O[j]:
                continue  # long side only in this template
            level = float(C[j] - retrace * body_j)
            hit = -1
            for p in range(j + 1, cur + 1):
                if L[p] <= level and C[p] > level:
                    hit = p
                    break
            if hit < 0 or ccur <= level:
                continue
            direction = "long"
            stop = min(float(L[hit]), level) - buf * a
            break

    if direction is None or stop is None:
        return None

    if ema is not None:
        if direction == "long" and ccur < ema:
            return None
        if direction == "short" and ccur > ema:
            return None

    entry = ccur  # HONEST: the decision bar's close
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    sign = 1.0 if direction == "long" else -1.0

    if p.get("target_mode", "rr") == "level" and target_level is not None:
        target = float(target_level)
        if sign * (target - entry) <= 0:
            target = entry + sign * rr * risk
    else:
        target = entry + sign * rr * risk

    return {"direction": direction, "entry": float(entry),
            "stop": float(stop), "target": float(target),
            "ts": bars["ts"].iloc[cur]}
