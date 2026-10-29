"""SWEEP -> DISPLACEMENT CONTINUATION with selectable POOL + NEW FILTERS.

Base architecture (validated, promoted candidate #7382 / pending #7383):
a liquidity POOL is taken (wick pierces, body closes back inside) and the
next displacement candle closes back THROUGH the pool level; entry is the
decision bar's CLOSE (honest: the live path submits a MARKET order, so the
close is the price the broker can actually pay -- finding #9134).  Stop sits
beyond the swept extreme; target is a fixed R multiple.

NEW SWITCHES added this cycle (2026-10-28d), all from this morning's ingest:

  trend_mode = 1   STRUCTURE TREND FILTER.
                   Sources: YT:OFcIcO-waug (Forex King, "liquidity strategy"):
                   "lower lows and lower highs = downtrend, higher highs and
                   higher lows = uptrend"; and YT:tFHyr0nRZaU (Neeraj joshi):
                   direction from market structure (HH-HL buy / LH-LL sell).
                   Rule: a LONG may only be taken when the two most recent
                   confirmed swing highs are rising AND the two most recent
                   confirmed swing lows are rising; a SHORT only when both
                   are falling.  This is the cheapest possible guard against
                   taking a sweep-continuation against the trend.

  ext_window > 0  EXTERNAL-ONLY LIQUIDITY.
                   Source: YT:OFcIcO-waug -- "avoid internal liquidity ...
                   internal liquidity is where the market keeps making highs
                   and lows inside the move ... avoid trading a sweep of a
                   strong pullback; go for external liquidity points."
                   Rule: the swept pool must ALSO be the extreme of the
                   previous `ext_window` bars, i.e. a MAJOR swing, not a minor
                   pullback level inside the range.

  prom_atr > 0    PROMINENCE QUALITY ON THE POOL.
                   Source: YT:AeKc0zxU6FY (FundedEnigma) -- equal highs/lows
                   and "old highs/lows that stand alone" are the pools price
                   runs.  Rule: the pool level must coincide with a confirmed
                   fractal pivot whose PROMINEENCE (distance to the opposite
                   extreme within `prom_win` bars) is >= prom_atr * ATR.

  pool_mode = "equal"   EQUAL-HIGHS/EQUAL-LOWS CLUSTER pool (2+ pivots at
                         nearly the same price).  Source: YT:AeKc0zxU6FY.

  mit50 = 1             STRUCTURAL INDUCEMENT filter (YT:qcqrxFG4wOA): only
                         take the continuation when the leg that produced the
                         pool has already been retraced >= 50% before the
                         sweep (a shift with an unmitigated prior leg is a
                         trap -> continuation of the impulse, not a reversal).

FILL / HYGIENE DISCIPLINE
-------------------------
  * entry = decision bar close (no retro-fill, no named structural price);
  * PLACEHOLDER bars (zero range or zero volume) are dropped INSIDE the maths
    before any level is computed -- they manufacture fake sweeps;
  * a geometry guard keeps the stop on the losing side and the target on the
    winning side of the entry.

STATELESS; numpy only (sandbox-safe).
"""
import numpy as np

DEFAULTS = {
    "setup": "sweep_cont",
    "pool_mode": "rolling",   # rolling | equal
    "lookback": 20,           # rolling pool window (bars)
    "ext_window": 0,          # >0: pool must also be the ext_window extreme
    "prom_atr": 0.0,          # >0: pool must be a pivot with this prominence
    "prom_win": 20,           # prominence look-back/forward window
    "trend_mode": 0,          # 1: structure trend filter (HH-HL / LH-LL)
    "pivot_len": 2,           # bars each side of a fractal pivot
    "mit50": 0,               # inducement (50% mitigation) filter
    "cluster_atr": 0.15,      # pivots within cluster_atr * ATR are "equal"
    "min_pivots": 2,          # pivots needed to call a pool an EQUAL pool
    "cluster_age": 40,        # equal pool's newest pivot must be this recent
    "min_sweep_atr": 0.60,    # sweep depth beyond the pool, in ATR
    "disp_k": 1.0,            # displacement bar range, in ATR
    "disp_mode": "through",   # through = close back through the LEVEL
    "body_ratio_min": 0.55,
    "rr": 2.0,
    "confirm_bars": 6,        # how many bars back the sweep may be
    "stop_buf_atr": 0.15,
    "atr_period": 14,
    "hist_bars": 320,         # bars of history the maths is allowed to see
}


def _cfg(params, key):
    try:
        if params is not None and key in params:
            return params[key]
    except TypeError:
        pass
    return DEFAULTS[key]


def _atr(H, L, C, period):
    n = len(C)
    if n < 3:
        return float("nan")
    pc = C[:-1]
    tr = np.maximum(H[1:] - L[1:],
                    np.maximum(np.abs(H[1:] - pc), np.abs(L[1:] - pc)))
    p = int(min(period, len(tr)))
    if p <= 0:
        return float("nan")
    return float(np.mean(tr[-p:]))


def _pivot_masks(H, L, pl):
    """Vectorised fractal pivots: strict, so plateaus do not double-count."""
    n = len(H)
    hi = np.ones(n, dtype=bool)
    lo = np.ones(n, dtype=bool)
    for o in range(1, int(pl) + 1):
        if o >= n:
            break
        hi[:n - o] &= H[o:] < H[:n - o]
        hi[o:] &= H[:n - o] < H[o:]
        lo[:n - o] &= L[o:] > L[:n - o]
        lo[o:] &= L[:n - o] > L[o:]
    return hi, lo


def _clusters(pivots, prices, tol):
    """Equal-price clusters: >=2 pivots within `tol` of each other."""
    out = []
    for ai, a in enumerate(pivots):
        members = [a]
        for b in pivots[ai + 1:]:
            if abs(prices[b] - prices[a]) <= tol:
                members.append(b)
        if len(members) >= 2:
            lvl = float(np.mean([prices[m] for m in members]))
            out.append((lvl, members[0], members[-1], len(members)))
    out.sort(key=lambda c: (-c[2], -c[3]))
    seen = []
    for c in out:
        if any(abs(c[0] - s[0]) <= tol * 0.5 for s in seen):
            continue
        seen.append(c)
    return seen


def _trend_ok(H, L, hi_idx, lo_idx, cur, direction, pl):
    """HH+HL for longs, LH+LL for shorts (confirmed pivots only)."""
    hi = [i for i in hi_idx if i <= cur - int(pl)]
    lo = [i for i in lo_idx if i <= cur - int(pl)]
    if len(hi) < 2 or len(lo) < 2:
        return False
    h1, h2 = hi[-1], hi[-2]
    l1, l2 = lo[-1], lo[-2]
    if direction == "long":
        return bool(H[h1] > H[h2] and L[l1] > L[l2])
    return bool(H[h1] < H[h2] and L[l1] < L[l2])


def _prom_ok(pivots, prices, level, H, L, win, need):
    """Is `level` a confirmed pivot with prominence >= need?"""
    for i in pivots:
        if abs(prices[i] - level) > need:
            continue
        lo = max(0, i - int(win))
        hi = min(len(H), i + int(win) + 1)
        if prices is None:
            continue
        prom = float(np.max(H[lo:hi])) - float(prices[i])
        if prom >= need:
            return True
    return False


def evaluate(bars, params):
    p = dict(DEFAULTS)
    try:
        p.update({k: v for k, v in (params or {}).items()
                  if k in DEFAULTS and v is not None})
    except Exception:
        pass

    pool_mode = str(_cfg(p, "pool_mode"))
    lookback = int(_cfg(p, "lookback"))
    ext_window = int(_cfg(p, "ext_window"))
    prom_atr = float(_cfg(p, "prom_atr"))
    prom_win = int(_cfg(p, "prom_win"))
    trend_mode = int(_cfg(p, "trend_mode"))
    pl = int(_cfg(p, "pivot_len"))
    tol_atr = float(_cfg(p, "cluster_atr"))
    min_pivots = int(_cfg(p, "min_pivots"))
    cluster_age = int(_cfg(p, "cluster_age"))
    ms_atr = float(_cfg(p, "min_sweep_atr"))
    disp_k = float(_cfg(p, "disp_k"))
    disp_mode = str(_cfg(p, "disp_mode"))
    min_body = float(_cfg(p, "body_ratio_min"))
    rr = float(_cfg(p, "rr"))
    confirm = int(_cfg(p, "confirm_bars"))
    buf_atr = float(_cfg(p, "stop_buf_atr"))
    atr_period = int(_cfg(p, "atr_period"))
    mit50 = int(_cfg(p, "mit50"))
    hist = int(_cfg(p, "hist_bars"))

    need = max(lookback, ext_window, cluster_age, prom_win) + confirm + atr_period + pl + 40
    if len(bars) < need:
        return None

    # --- placeholder-bar hygiene -------------------------------------------
    try:
        valid = ((bars["high"] - bars["low"]) > 0) & (bars["volume"] > 0)
        b = bars[valid]
    except Exception:
        b = bars
    b = b.tail(hist) if len(b) > hist else b
    n = len(b)
    if n < 60:
        return None
    cur = n - 1

    H = b["high"].to_numpy(float)
    L = b["low"].to_numpy(float)
    C = b["close"].to_numpy(float)
    O = b["open"].to_numpy(float)

    a = _atr(H, L, C, atr_period)
    if not np.isfinite(a) or a <= 0:
        return None
    tol = tol_atr * a
    ms = ms_atr * a
    buf = buf_atr * a

    ccur = float(C[cur])
    hcur = float(H[cur])
    lcur = float(L[cur])
    rng = max(hcur - lcur, 1e-9)
    br = abs(ccur - float(O[cur])) / rng

    # cheap rejection first: the decision bar must be a displacement candle
    if br < min_body or rng < disp_k * a:
        return None

    need_pivots = (pool_mode == "equal") or mit50 or trend_mode or (prom_atr > 0)
    highs = []
    lows = []
    if need_pivots:
        hi_mask, lo_mask = _pivot_masks(H, L, pl)
        highs = [int(i) for i in np.flatnonzero(hi_mask)]
        lows = [int(i) for i in np.flatnonzero(lo_mask)]

    direction = None
    stop = None

    # ------------------------------------------------ rolling-extreme pool --
    if pool_mode == "rolling":
        j = -1
        for k in range(cur - 1, max(0, cur - confirm - 1), -1):
            if k - lookback < 0:
                continue
            phk = float(np.max(H[k - lookback:k]))
            plk = float(np.min(L[k - lookback:k]))
            if L[k] < plk - ms and C[k] > plk:
                j = k
                break
            if H[k] > phk + ms and C[k] < phk:
                j = k
                break
        if j < 0:
            return None
        plj = float(np.min(L[j - lookback:j]))
        phj = float(np.max(H[j - lookback:j]))
        if L[j] < plj - ms and C[j] > plj:
            # EXTERNAL-only: the pool must also be the ext_window extreme
            if ext_window > lookback and j - ext_window >= 0:
                ext_lo = float(np.min(L[j - ext_window:j]))
                if plj > ext_lo + 1e-9:
                    return None
            if prom_atr > 0:
                piv = [i for i in lows if abs(L[i] - plj) <= prom_atr * a]
                if not _prom_ok(piv, L, plj, H, L, prom_win, prom_atr * a):
                    return None
            if mit50:
                prior = [i for i in highs if i < j - lookback]
                if prior:
                    leg_high = float(H[prior[-1]])
                    mid = 0.5 * (leg_high + plj)
                    if float(np.max(H[j - lookback:j + 1])) < mid:
                        return None
            if disp_mode in ("through", "either"):
                ok = ccur > plj
            else:
                ok = ccur > float(L[j])
            if not ok:
                return None
            if trend_mode and not _trend_ok(H, L, highs, lows, cur, "long", pl):
                return None
            direction = "long"
            stop = float(L[j]) - buf
        else:
            if ext_window > lookback and j - ext_window >= 0:
                ext_hi = float(np.max(H[j - ext_window:j]))
                if phj < ext_hi - 1e-9:
                    return None
            if prom_atr > 0:
                piv = [i for i in highs if abs(H[i] - phj) <= prom_atr * a]
                if not _prom_ok(piv, H, phj, H, L, prom_win, prom_atr * a):
                    return None
            if mit50:
                prior = [i for i in lows if i < j - lookback]
                if prior:
                    leg_low = float(L[prior[-1]])
                    mid = 0.5 * (leg_low + phj)
                    if float(np.min(L[j - lookback:j + 1])) > mid:
                        return None
            if disp_mode in ("through", "either"):
                ok = ccur < phj
            else:
                ok = ccur < float(H[j])
            if not ok:
                return None
            if trend_mode and not _trend_ok(H, L, highs, lows, cur, "short", pl):
                return None
            direction = "short"
            stop = float(H[j]) + buf

    # ----------------------------------------------------- equal-pool mode --
    elif pool_mode == "equal":
        if len(lows) >= min_pivots:
            for lvl, first_i, last_i, cnt in _clusters(lows, L, tol):
                if (cur - last_i) > cluster_age:
                    continue
                j = -1
                for k in range(cur - 1, max(0, cur - confirm - 1), -1):
                    if k <= last_i:
                        break
                    if L[k] < lvl - ms and C[k] > lvl:
                        j = k
                        break
                if j < 0:
                    continue
                if disp_mode in ("through", "either"):
                    ok = ccur > lvl
                else:
                    ok = ccur > float(L[j])
                if not ok:
                    continue
                if mit50:
                    prior = [i for i in highs if i < first_i]
                    if prior:
                        mid = 0.5 * (float(H[prior[-1]]) + lvl)
                        if float(np.max(H[last_i:j + 1])) < mid:
                            continue
                if trend_mode and not _trend_ok(H, L, highs, lows, cur, "long", pl):
                    continue
                direction = "long"
                stop = float(L[j]) - buf
                break

        if direction is None and len(highs) >= min_pivots:
            for lvl, first_i, last_i, cnt in _clusters(highs, H, tol):
                if (cur - last_i) > cluster_age:
                    continue
                j = -1
                for k in range(cur - 1, max(0, cur - confirm - 1), -1):
                    if k <= last_i:
                        break
                    if H[k] > lvl + ms and C[k] < lvl:
                        j = k
                        break
                if j < 0:
                    continue
                if disp_mode in ("through", "either"):
                    ok = ccur < lvl
                else:
                    ok = ccur < float(H[j])
                if not ok:
                    continue
                if mit50:
                    prior = [i for i in lows if i < first_i]
                    if prior:
                        mid = 0.5 * (float(L[prior[-1]]) + lvl)
                        if float(np.min(L[last_i:j + 1])) > mid:
                            continue
                if trend_mode and not _trend_ok(H, L, highs, lows, cur, "short", pl):
                    continue
                direction = "short"
                stop = float(H[j]) + buf
                break
    else:
        return None

    if direction is None or stop is None:
        return None

    entry = ccur                      # HONEST: the decision bar's close
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    sign = 1.0 if direction == "long" else -1.0
    target = entry + sign * rr * risk

    # geometry guard: stop on the losing side, target on the winning side
    if sign * (stop - entry) >= 0 or sign * (target - entry) <= 0:
        return None

    return {"direction": direction, "entry": float(entry),
            "stop": float(stop), "target": float(target),
            "ts": b["ts"].iloc[cur]}
