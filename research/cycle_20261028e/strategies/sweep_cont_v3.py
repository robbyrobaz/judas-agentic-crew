"""SWEEP -> DISPLACEMENT CONTINUATION v3 (2026-10-28e cycle).

Parent architecture: research/cycle_20261028d/strategies/sweep_pool_filters_v2.py
(the family behind promoted candidate #7382 / pending #7383).

A liquidity POOL is taken (wick pierces the level, body closes back inside)
and a later displacement candle closes back THROUGH the pool level.  Entry is
the DECISION BAR'S CLOSE -- honest: the live path submits a MARKET order, so
the close is a price the broker can actually pay (finding #9134).  Stop sits
beyond the swept extreme; target is a fixed R multiple.

NEW AXES ADDED THIS CYCLE (2026-10-28e) -- all from ingest that had not yet
been turned into a filter:

  sess_mode       SESSION KILL-ZONE GATE.
                  Source: YT:rZnjkZ_JV2o (Mulham Trading) -- "trade only inside
                  the kill zones: London 06:00-09:00 UTC and New York
                  11:00-14:00 UTC"; YT:JWDA-PBYutM (Rk Bulls) -- 15m context,
                  5m setup, max 2 trades/day.
                  0 = any time, 1 = London only (06-09 UTC), 2 = NY only
                  (11-14 UTC), 3 = both kill zones, 4 = NY AM only (11-13 UTC),
                  5 = everything EXCEPT the kill zones.

  sweep_body_max  MAX BODY RATIO OF THE SWEEP CANDLE (default 1.0 = off).
                  Source: YT:tFHyr0nRZaU (Neeraj joshi) -- "a sweep is a wick
                  that takes the previous swing high/low but the candle does
                  NOT close beyond it"; the distinguishing evidence is the
                  WICK, not the body.
                  Rule: |C-O| / (H-L) of the sweep candle must be <= this
                  value, i.e. the sweep must be a rejection wick rather than a
                  body-driven break.

  target_mode     0 = fixed R multiple (rr).
                  1 = NEXT POOL (no look-ahead): for a long the target is the
                  highest high of the `tgt_window` bars BEFORE the decision
                  bar -- a pool that already exists above us; for a short the
                  lowest low.  Source: YT:l27wF-EvDkw (TradeIQ) -- "target =
                  the NEXT POOL of liquidity, not a fixed R multiple" and
                  YT:AeKc0zxU6FY (FundedEnigma) -- equal highs/lows are the
                  pools price runs to.  A trade whose pool is closer than
                  min_rr * risk is SKIPPED (min_rr floor), so the target is
                  never closer than the fixed-R alternative.

FILL / HYGIENE DISCIPLINE (unchanged, and it is the whole point):
  * entry = decision bar close (no retro-fill, no named structural price);
  * PLACEHOLDER bars (zero range or zero volume) are dropped INSIDE the maths
    before any level is computed -- they manufacture fake sweeps;
  * if the DECISION bar itself is a placeholder, no signal is emitted at all
    (otherwise the house engine would book a signal whose entry belongs to an
    older bar -- a retro-fill);
  * a geometry guard keeps the stop on the losing side and the target on the
    winning side of the entry.

STATELESS; numpy + pandas only (sandbox-safe).
"""
import numpy as np
import pandas as pd

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
    # --- new in 2026-10-28e ---
    "sess_mode": 0,           # session kill-zone gate (see module docstring)
    "sweep_body_max": 1.0,    # max body ratio of the sweep candle (1.0 = off)
    "target_mode": 0,         # 0 = fixed R, 1 = next pool above/below
    "tgt_window": 60,         # bars searched for the next pool
    "min_rr": 0.5,            # floor on pool distance when target_mode = 1
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
    tr = np.maximum(H[1:] - L[1:], np.maximum(np.abs(H[1:] - pc), np.abs(L[1:] - pc)))
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
        prom = float(np.max(H[lo:hi])) - float(prices[i])
        if prom >= need:
            return True
    return False


def _session_ok(mode, ts):
    """Kill-zone gate on the DECISION bar's UTC hour."""
    m = int(mode)
    if m == 0:
        return True
    try:
        hour = int(pd.Timestamp(ts).hour)
    except Exception:
        return False
    if hour < 0:
        return False
    london = 6 <= hour < 9
    ny = 11 <= hour < 14
    if m == 1:
        return london
    if m == 2:
        return ny
    if m == 3:
        return london or ny
    if m == 4:
        return 11 <= hour < 13
    if m == 5:
        return not (london or ny)
    return True


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
    sess_mode = int(_cfg(p, "sess_mode"))
    sweep_body_max = float(_cfg(p, "sweep_body_max"))
    target_mode = int(_cfg(p, "target_mode"))
    tgt_window = int(_cfg(p, "tgt_window"))
    min_rr = float(_cfg(p, "min_rr"))

    need = max(lookback, ext_window, cluster_age, prom_win, tgt_window) + confirm + atr_period + pl + 40
    if len(bars) < need:
        return None

    # --- placeholder-bar hygiene -------------------------------------------
    try:
        valid = ((bars["high"] - bars["low"]) > 0) & (bars["volume"] > 0)
        if not bool(valid.iloc[-1]):
            # The DECISION bar is a placeholder -> no signal.  Emitting one
            # would make the engine fill at an older bar's close (retro-fill).
            return None
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

    ts_cur = b["ts"].iloc[cur]
    if not _session_ok(sess_mode, ts_cur):
        return None

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

    def _sweep_body_ok(j):
        if sweep_body_max >= 1.0:
            return True
        rj = float(H[j]) - float(L[j])
        if rj <= 0:
            return False
        return abs(float(C[j]) - float(O[j])) / rj <= sweep_body_max

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
            if not _sweep_body_ok(j):
                return None
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
            if not _sweep_body_ok(j):
                return None
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
                if not _sweep_body_ok(j):
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
                if not _sweep_body_ok(j):
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

    if target_mode == 1:
        # NEXT POOL, no look-ahead: the extreme of the tgt_window bars BEFORE
        # the decision bar.  Skipped when it is closer than min_rr * risk.
        lo_w = max(0, cur - tgt_window + 1)
        win_h = H[lo_w:cur]
        win_l = L[lo_w:cur]
        if len(win_h) == 0:
            return None
        if direction == "long":
            pool = float(np.max(win_h))
        else:
            pool = float(np.min(win_l))
        dist = sign * (pool - entry)
        if dist <= 0 or dist < min_rr * risk:
            return None
        target = pool
    else:
        target = entry + sign * rr * risk

    # geometry guard: stop on the losing side, target on the winning side
    if sign * (stop - entry) >= 0 or sign * (target - entry) <= 0:
        return None

    return {"direction": direction, "entry": float(entry),
            "stop": float(stop), "target": float(target),
            "ts": ts_cur}
