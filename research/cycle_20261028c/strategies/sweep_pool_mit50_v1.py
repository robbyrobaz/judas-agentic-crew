"""SWEEP -> DISPLACEMENT CONTINUATION with a selectable LIQUIDITY POOL and an
optional 50%-MITIGATION (inducement) filter -- market entry, v1.

WHY THIS FILE
-------------
The ONE family in this book that survived the honest-fill audit
(#9145/#9146, candidates 7382/7383) is the MARKET-ENTRY sweep -> displacement
continuation: the decision bar's CLOSE is the entry, so there is no named
structural price to fill against retroactively.

This file keeps that architecture and makes two things switchable:

  pool_mode = "rolling"   the pool is the rolling extreme of the last
                           `lookback` bars (the promoted MGC cell, csid 439/441).
  pool_mode = "equal"     the pool is an EQUAL-HIGHS / EQUAL-LOWS CLUSTER --
                           2+ swing pivots sitting at nearly the same price.
                           NEW RULE from YT:AeKc0zxU6FY (FundedEnigma,
                           "How to Find Liquidity Like a Pro"): "Two or more
                           swing points sitting at nearly the same price" are
                           equal highs/lows; equal levels stack more stops and
                           are the pools price runs.

  mit50 = 1               STRUCTURAL INDUCEMENT filter, NEW RULE from
                           YT:qcqrxFG4wOA (PRINCE TRADE X, "Liquidity &
                           Inducement Trap"): a break of structure that never
                           retraced the 50% level of the previous impulse is a
                           trap.  We therefore only take the continuation when
                           the leg that produced the pool has ALREADY been
                           mitigated by >= 50% BEFORE the sweep -- i.e. the
                           sweep happens in DISCOUNT (long) / PREMIUM (short).
                           The leg is defined by PIVOTS so the test does not
                           depend on how much history the window carries.

  YT:tFHyr0nRZaU (Neeraj joshi, Market Structure & Liquidity Sweep
  Masterclass Ep.2, 5m chart): the trigger is the sweep of the swing followed
  by a candle that CLOSES back through the swept level; the stop goes beyond
  the swept extreme.  Both are implemented below.

FILL DISCIPLINE (findings 9134 / 9145 / 9146)
---------------------------------------------
The live path submits a MARKET order; there is no LIMIT entry path anywhere
in this repo.  So the ENTRY is the decision bar's CLOSE -- the price the
broker can actually pay after the decision.  Stops and targets stay
structural; only the entry is honest.  A GEOMETRY GUARD keeps the stop on the
losing side of the entry and the target on the winning side.

PLACEHOLDER-BAR FILTER
-----------------------
The deep archive is padded with PLACEHOLDER bars (open=high=low=close at a
stale price, volume 0) wherever IBKR history was missing.  Those bars
manufacture fake sweeps and fake displacement candles, so the maths only ever
looks at bars with a real range and real volume.

STATELESS; numpy only (sandbox-safe).
"""
import numpy as np

DEFAULTS = {
    # --- cell (the house grid substitutes this literal) ---
    "setup": "sweep_cont",
    "pool_mode": "rolling",   # rolling | equal
    "lookback": 20,           # rolling pool window (bars)
    "pivot_len": 2,           # bars each side of a fractal pivot
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
    "mit50": 0,               # inducement filter (see docstring)
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
    """Equal-price clusters: >=2 pivots within `tol` of each other.

    Returns (level, first_idx, last_idx, count), newest cluster first.
    """
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


def evaluate(bars, params):
    p = dict(DEFAULTS)
    try:
        p.update({k: v for k, v in (params or {}).items()
                  if k in DEFAULTS and v is not None})
    except Exception:
        pass

    pool_mode = str(_cfg(p, "pool_mode"))
    lookback = int(_cfg(p, "lookback"))
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

    need = max(lookback, cluster_age) + confirm + atr_period + pl + 40
    if len(bars) < need:
        return None

    # --- placeholder-bar hygiene: keep only bars with a real range/volume ---
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

    highs = []
    lows = []
    if pool_mode == "equal" or mit50:
        hi_mask, lo_mask = _pivot_masks(H, L, pl)
        highs = list(np.flatnonzero(hi_mask))
        lows = list(np.flatnonzero(lo_mask))

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
            if disp_mode in ("through", "either"):
                ok = ccur > plj
            else:
                ok = ccur > float(L[j])
            if not ok:
                return None
            if mit50:
                prior = [i for i in highs if i < j - lookback]
                if prior:
                    leg_high = float(H[prior[-1]])
                    mid = 0.5 * (leg_high + plj)
                    if float(np.max(H[j - lookback:j + 1])) < mid:
                        return None
            direction = "long"
            stop = float(L[j]) - buf
        else:
            if disp_mode in ("through", "either"):
                ok = ccur < phj
            else:
                ok = ccur < float(H[j])
            if not ok:
                return None
            if mit50:
                prior = [i for i in lows if i < j - lookback]
                if prior:
                    leg_low = float(L[prior[-1]])
                    mid = 0.5 * (leg_low + phj)
                    if float(np.min(L[j - lookback:j + 1])) > mid:
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
