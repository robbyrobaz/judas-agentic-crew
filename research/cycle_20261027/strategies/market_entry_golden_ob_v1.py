"""MARKET-ENTRY GOLDEN-FVG / ORDER-BLOCK-PINBAR strategy (2026-10-27 cycle).

SOURCE
------
YT:_EJIacX5VNk ("Learn ICT Concepts in 20 Minutes", Pro Trading School).
The video states two mechanical trade sequences that are NOT in the book yet:

  (A) GOLDEN FVG
      1. read the structure (CHoCH then BOS),
      2. take the impulsive leg that caused the break,
      3. the leg leaves a 3-candle FVG,
      4. the FVG must ALIGN with the 50% or 61.8% retracement of that leg
         ("golden fair value gap") -- fib is used to MEASURE the leg, not to
         predict,
      5. wait for price to retrace INTO the FVG,
      6. wait for the REACTION (the market rebalancing the imbalance and
         then rejecting) -- "that reaction is what provides confirmation",
      7. enter, stop beyond the far side of the zone, target >= 2R.

  (B) ORDER BLOCK
      1. the OB is the LAST OPPOSITE candle before the impulsive move that
         created the FVG,
      2. wait for price to trade back INTO the OB,
      3. wait for a PIN BAR rejection inside the OB ("buyers stepped back
         into the market"),
      4. "Entry at the close of the pin bar",
      5. "Stop loss, below the low of the order block",
      6. "Target, a minimum reward to risk ratio of two to one",
      7. the higher timeframe structure must agree with the trade.

WHY EVERY ENTRY HERE IS THE DECISION BAR'S CLOSE
------------------------------------------------
Finding #9145/#9146: the house backtester fills AT THE PRICE THE STRATEGY
NAMES, so any family that names a structural price (FVG midpoint, OB
midpoint, ...) carries a retro-fill premium that a live market order can
never collect.  Both sequences above are re-stated in the video as CLOSE
ENTRIES ("entry at the close of the pin bar", "wait for the reaction"), so
the structural level is used ONLY for the STOP and for the zone test; the
entry is always the close of the bar that produced the confirmation.  That
makes base / fair / live agree, which is the only kind of PF the broker can
actually pay.

Stateless; numpy only (sandbox-safe).
"""
import numpy as np


def _atr_array(H, L, C, period):
    """Per-bar ATR (rolling mean of true range), fully vectorised."""
    n = len(C)
    prev = np.empty(n)
    prev[0] = C[0]
    prev[1:] = C[:-1]
    tr = np.maximum(H - L, np.maximum(np.abs(H - prev), np.abs(L - prev)))
    cs = np.cumsum(tr)
    pre = np.concatenate(([0.0], cs[:-1]))
    idx = np.arange(n)
    lo = np.maximum(0, idx - period + 1)
    return (cs - pre[lo]) / (idx - lo + 1).astype(float)


def _ema(v, period):
    alpha = 2.0 / (float(period) + 1.0)
    out = float(v[0])
    for x in v[1:]:
        out += alpha * (float(x) - out)
    return out


def evaluate(bars, params):
    setup = str(params.get("setup", "golden_fvg"))
    n = len(bars)
    if n < 60:
        return None
    cur = n - 1

    O_all = bars["open"].to_numpy(float)
    H_all = bars["high"].to_numpy(float)
    L_all = bars["low"].to_numpy(float)
    C_all = bars["close"].to_numpy(float)

    # ---- work on a bounded window so every call is O(window) -----------
    W = int(params.get("window", 140))
    start = max(0, cur - W + 1)
    O = O_all[start:]
    H = H_all[start:]
    L = L_all[start:]
    C = C_all[start:]
    m = len(C)
    if m < 40:
        return None
    cur = m - 1

    a_period = int(params.get("atr_period", 14))
    atr = _atr_array(H, L, C, a_period)
    a = float(atr[cur])
    if not np.isfinite(a) or a <= 0:
        return None

    rng = H - L
    with np.errstate(divide="ignore", invalid="ignore"):
        br = np.where(rng > 0, np.abs(C - O) / np.where(rng > 0, rng, 1.0), 0.0)

    min_body = float(params.get("body_ratio_min", 0.55))
    disp_k = float(params.get("disp_k", 1.0))
    buf = float(params.get("stop_buf_atr", 0.15))
    rr = float(params.get("rr", 2.0))
    lb = int(params.get("impulse_lookback", 20))
    fibs = params.get("fibs", [0.5, 0.618])
    fibs = [float(f) for f in fibs]
    react_within = int(params.get("react_within", 1))
    pin_wick = float(params.get("pin_wick_frac", 0.5))
    wick_only = bool(params.get("wick_reject", False))

    oc, hc, lc, cc = O[cur], H[cur], L[cur], C[cur]
    rng_c = max(hc - lc, 1e-9)

    direction = None
    stop = None

    # ------------------------------------------------------------------ A
    if setup == "golden_fvg":
        # candidate impulse bars: displacement in the trade direction
        up_imp = (C > O) & (br >= min_body) & (rng >= disp_k * atr)
        dn_imp = (C < O) & (br >= min_body) & (rng >= disp_k * atr)

        # FVG (3 candles): bullish if low[c3] > high[c1]; bearish if high[c3] < low[c1]
        fvg_up = np.zeros(m, dtype=bool)
        fvg_dn = np.zeros(m, dtype=bool)
        if m >= 3:
            fvg_up[: m - 2] = L[2:] > H[: m - 2]
            fvg_dn[: m - 2] = H[2:] < L[: m - 2]

        # the FVG must be formed at least `react_within` bars before the
        # reaction bar, and the reaction bar is the current bar (or close to it)
        for j in range(m - 3, max(-1, m - 4 - lb), -1):
            if j < 1:
                break
            if up_imp[j] and fvg_up[j]:
                zlo, zhi = float(H[j]), float(L[j + 2])
                if zhi <= zlo:
                    continue
                lo0 = max(0, j - lb + 1)
                leg_lo = float(np.min(L[lo0:j + 1]))
                leg_hi = float(np.max(H[j:j + 3]))
                if leg_hi <= leg_lo:
                    continue
                golden = False
                for f in fibs:
                    lvl = leg_hi - f * (leg_hi - leg_lo)
                    if zlo <= lvl <= zhi:
                        golden = True
                if not golden:
                    continue
                # price must have retraced INTO the zone at some point after
                # the gap formed
                touched = -1
                for q in range(j + 3, m):
                    if L[q] <= zhi:
                        touched = q
                        break
                if touched < 0:
                    continue
                if cur - touched > react_within:
                    continue
                # reaction bar = current bar: trades into the zone and closes
                # back above its bottom
                if not (lc <= zhi and cc > zlo):
                    continue
                if cc <= oc:
                    continue
                if wick_only:
                    body_lo = min(oc, cc)
                    if (body_lo - lc) < pin_wick * rng_c:
                        continue
                direction, stop = "long", min(lc, zlo) - buf * a
                break
            elif dn_imp[j] and fvg_dn[j]:
                zlo, zhi = float(H[j + 2]), float(L[j])
                if zhi <= zlo:
                    continue
                lo0 = max(0, j - lb + 1)
                leg_lo = float(np.min(L[lo0:j + 1]))
                leg_hi = float(np.max(H[j:j + 3]))
                if leg_hi <= leg_lo:
                    continue
                golden = False
                for f in fibs:
                    lvl = leg_lo + f * (leg_hi - leg_lo)
                    if zlo <= lvl <= zhi:
                        golden = True
                if not golden:
                    continue
                touched = -1
                for q in range(j + 3, m):
                    if H[q] >= zlo:
                        touched = q
                        break
                if touched < 0:
                    continue
                if cur - touched > react_within:
                    continue
                if not (hc >= zlo and cc < zhi):
                    continue
                if cc >= oc:
                    continue
                if wick_only:
                    body_hi = max(oc, cc)
                    if (hc - body_hi) < pin_wick * rng_c:
                        continue
                direction, stop = "short", max(hc, zhi) + buf * a
                break

    # ------------------------------------------------------------------ B
    elif setup == "ob_pinbar":
        up_imp = (C > O) & (br >= min_body) & (rng >= disp_k * atr)
        dn_imp = (C < O) & (br >= min_body) & (rng >= disp_k * atr)
        for j in range(m - 2, max(0, m - 3 - lb), -1):
            ob = j - 1
            if ob < 0:
                break
            if up_imp[j] and C[ob] < O[ob]:
                zlo, zhi = float(L[ob]), float(H[ob])
                if zhi <= zlo:
                    continue
                hit = -1
                for q in range(j, m):
                    if L[q] <= zhi and H[q] >= zlo:
                        hit = q
                        break
                if hit < 0:
                    continue
                if cur - hit > react_within:
                    continue
                if not (lc <= zhi and hc >= zlo):
                    continue
                if cc <= oc:
                    continue
                body_lo = min(oc, cc)
                if (body_lo - lc) < pin_wick * rng_c:
                    continue
                direction, stop = "long", zlo - buf * a
                break
            elif dn_imp[j] and C[ob] > O[ob]:
                zlo, zhi = float(L[ob]), float(H[ob])
                if zhi <= zlo:
                    continue
                hit = -1
                for q in range(j, m):
                    if L[q] <= zhi and H[q] >= zlo:
                        hit = q
                        break
                if hit < 0:
                    continue
                if cur - hit > react_within:
                    continue
                if not (lc <= zhi and hc >= zlo):
                    continue
                if cc >= oc:
                    continue
                body_hi = max(oc, cc)
                if (hc - body_hi) < pin_wick * rng_c:
                    continue
                direction, stop = "short", zhi + buf * a
                break

    if direction is None or stop is None:
        return None

    # optional higher-timeframe proxy: only trade with the trend
    ema_p = int(params.get("ema_period", 0))
    if ema_p > 0:
        ema = _ema(C, ema_p)
        if direction == "long" and cc < ema:
            return None
        if direction == "short" and cc > ema:
            return None

    entry = float(cc)          # HONEST: the decision bar's close
    risk = abs(entry - float(stop))
    if risk <= 0:
        return None
    sign = 1.0 if direction == "long" else -1.0
    target = entry + sign * rr * risk
    if sign * (target - entry) <= 0:
        return None

    return {"direction": direction, "entry": entry,
            "stop": float(stop), "target": float(target),
            "ts": bars["ts"].iloc[n - 1]}
