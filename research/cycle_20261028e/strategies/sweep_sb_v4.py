"""SWEEP family v4 (2026-10-28e) -- TWO setups, both honest-fill.

Parent: research/cycle_20261028e/strategies/sweep_cont_v3.py (continuation) and
research/cycle_20261028d/strategies/sweep_pool_filters_v2.py.

SETUP "cont"  (unchanged architecture, v3 rules)
    liquidity POOL (rolling window) is taken -> displacement candle closes back
    THROUGH the pool -> continuation.

SETUP "sb_day"  (NEW ARCHITECTURE -- ICT SILVER BULLET AT THE DAY'S EXTREMES)
    Source: YT:r5rRJ0GKxA0 (Com Lucro Trader, "ICT Silver Bullet Explained:
    Liquidity Sweeps, MSS and FVG Entries", 4:45, manual en, 2026-10-28).
    Mechanical rules taken from that video:
      1. "Wait for price to break above the day's high ... and then quickly
          return inside the range, taking out the buy side liquidity."
      2. "A bearish market structure shift happens when price breaks and closes
          below the most recent swing low."  (bullish mirror)
      3. Entry into the fair value gap left by the displacement; the model
          trades the REACTION after the trap.
    Implementation:
      * pool  = the PREVIOUS SESSION DAY's high (short) / low (long) -- a real
        calendar day's extreme, computed from the day's own bars, never from a
        named price the market has already closed through;
      * sweep = a bar that pierces beyond that extreme by min_sweep_atr * ATR
        and closes back INSIDE the day's range (rejection);
      * confirmation (mss_mode=1) = the decision bar closes beyond the most
        recent CONFIRMED swing point (structure shift), and it is a
        displacement bar (range >= disp_k * ATR, body ratio >= body_ratio_min);
      * entry = the decision bar's CLOSE (honest market fill -- the live path
        submits a MARKET order, finding #9134);
      * stop = beyond the sweep extreme; target = rr * risk, or the next pool
        (target_mode=1).

NEW AXES (both setups):
  mss_mode  1 = require the structure shift (close beyond the last confirmed
               swing) on the decision bar.
  vol_k     >0 = require the DECISION bar's volume >= vol_k * median volume of
               the last vol_win bars ("especially during high volume
               sessions" -- YT:r5rRJ0GKxA0; "volume confirmation" --
               YT:aDoJiGRRuh0).

HYGIENE (unchanged, and it is the whole point):
  * placeholder bars (zero range or zero volume) are dropped before any level
    is computed -- they manufacture fake sweeps;
  * if the DECISION bar itself is a placeholder, no signal at all;
  * geometry guard: stop on the losing side, target on the winning side.

STATELESS; numpy + pandas only (sandbox-safe).
"""
import numpy as np
import pandas as pd

DEFAULTS = {
    "setup": "cont",           # cont | sb_day
    # ---- shared structure/displacement params -------------------------
    "lookback": 20,            # rolling pool window (bars) -- setup "cont"
    "ext_window": 0,
    "prom_atr": 0.0,
    "prom_win": 20,
    "trend_mode": 0,
    "pivot_len": 2,
    "mit50": 0,
    "cluster_atr": 0.15,
    "min_pivots": 2,
    "cluster_age": 40,
    "min_sweep_atr": 0.60,     # sweep depth beyond the pool, in ATR
    "disp_k": 1.0,             # displacement bar range, in ATR
    "disp_mode": "through",
    "body_ratio_min": 0.55,
    "rr": 2.0,
    "confirm_bars": 6,         # how many bars back the sweep may be
    "stop_buf_atr": 0.15,
    "atr_period": 14,
    "hist_bars": 320,
    "sess_mode": 0,
    "sweep_body_max": 1.0,
    "target_mode": 0,
    "tgt_window": 60,
    "min_rr": 0.5,
    # ---- new in v4 ----------------------------------------------------
    "mss_mode": 0,             # 1 = require the structure shift
    "mss_pl": 2,               # fractal width for the MSS swing
    "vol_k": 0.0,              # displacement volume >= vol_k * median vol
    "vol_win": 100,
    "day_window": 1500,        # bars searched for the previous session day
    "min_day_bars": 60,        # a session day needs this many real bars
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


def _day_extremes(b, day_window, min_day_bars, cur_day):
    """(high, low) of the most recent COMPLETE session day before cur_day.

    `b` is the placeholder-free frame (sorted by ts).  Returns (None, None)
    when no previous day has enough real bars to be a session.
    """
    w = b.tail(int(day_window))
    if len(w) < int(min_day_bars):
        return None, None
    ts = w["ts"]
    try:
        day_ids = ts.values.astype("datetime64[D]").astype("int64")
    except Exception:
        try:
            day_ids = ts.dt.floor("D").astype("int64").to_numpy()
        except Exception:
            return None, None
    cur = int(cur_day)
    days = day_ids[day_ids < cur]
    if len(days) == 0:
        return None, None
    uniq, counts = np.unique(days, return_counts=True)
    ok = uniq[counts >= int(min_day_bars)]
    if len(ok) == 0:
        return None, None
    prev = int(ok.max())
    mask = day_ids == prev
    if not bool(mask.any()):
        return None, None
    H = w["high"].to_numpy(float)
    L = w["low"].to_numpy(float)
    return float(np.max(H[mask])), float(np.min(L[mask]))


def _mss_ok(H, L, hi_idx, lo_idx, cur, direction, pl):
    """Structure shift: the decision bar closes beyond the last CONFIRMED swing."""
    if direction == "long":
        cand = [i for i in lo_idx if i <= cur - int(pl)]
        if not cand:
            return False
        lvl = float(L[cand[-1]])
        return float(H[cur]) > lvl
    cand = [i for i in hi_idx if i <= cur - int(pl)]
    if not cand:
        return False
    lvl = float(H[cand[-1]])
    return float(L[cur]) < lvl


def evaluate(bars, params):
    p = dict(DEFAULTS)
    try:
        p.update({k: v for k, v in (params or {}).items()
                  if k in DEFAULTS and v is not None})
    except Exception:
        pass

    setup = str(_cfg(p, "setup"))
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
    mss_mode = int(_cfg(p, "mss_mode"))
    mss_pl = int(_cfg(p, "mss_pl"))
    vol_k = float(_cfg(p, "vol_k"))
    vol_win = int(_cfg(p, "vol_win"))
    day_window = int(_cfg(p, "day_window"))
    min_day_bars = int(_cfg(p, "min_day_bars"))

    need = max(lookback, ext_window, cluster_age, prom_win, tgt_window) + confirm + atr_period + pl + 40
    if len(bars) < need:
        return None

    # --- placeholder-bar hygiene -------------------------------------------
    try:
        valid = ((bars["high"] - bars["low"]) > 0) & (bars["volume"] > 0)
        if not bool(valid.iloc[-1]):
            return None
        bvalid = bars[valid]
    except Exception:
        bvalid = bars
    b = bvalid.tail(hist) if len(bvalid) > hist else bvalid
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

    # the decision bar must be a displacement candle
    if br < min_body or rng < disp_k * a:
        return None

    # volume confirmation (decision bar)
    if vol_k > 0:
        try:
            V = b["volume"].to_numpy(float)
            vw = V[max(0, cur - vol_win + 1):cur]
            if len(vw) < 20:
                return None
            med = float(np.median(vw))
            if med <= 0 or float(V[cur]) < vol_k * med:
                return None
        except Exception:
            return None

    need_pivots = (setup == "sb_day") or trend_mode or mit50 or (prom_atr > 0)
    highs = []
    lows = []
    if need_pivots:
        hi_mask, lo_mask = _pivot_masks(H, L, max(pl, mss_pl))
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

    if setup == "sb_day":
        # -------- ICT SILVER BULLET at the previous session day's extremes --
        try:
            cur_day = pd.Timestamp(ts_cur).floor("D")
            cur_day_id = int(cur_day.value // 86400000000000)
        except Exception:
            return None
        ph, pll = _day_extremes(bvalid, day_window, min_day_bars, cur_day_id)
        if ph is None or pll is None:
            return None

        # --- bullish: sell-side sweep of the previous day's LOW ------------
        for k in range(cur - 1, max(0, cur - confirm - 1), -1):
            if L[k] < pll - ms and C[k] > pll:
                if not _sweep_body_ok(k):
                    continue
                if ccur <= pll:
                    continue
                if mss_mode and not _mss_ok(H, L, highs, lows, cur, "long", mss_pl):
                    continue
                direction = "long"
                stop = float(L[k]) - buf
                break
        # --- bearish: buy-side sweep of the previous day's HIGH ------------
        if direction is None:
            for k in range(cur - 1, max(0, cur - confirm - 1), -1):
                if H[k] > ph + ms and C[k] < ph:
                    if not _sweep_body_ok(k):
                        continue
                    if ccur >= ph:
                        continue
                    if mss_mode and not _mss_ok(H, L, highs, lows, cur, "short", mss_pl):
                        continue
                    direction = "short"
                    stop = float(H[k]) + buf
                    break

    else:
        # ------------------------------------------------ rolling-extreme pool --
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
            ok = ccur > plj if disp_mode in ("through", "either") else ccur > float(L[j])
            if not ok:
                return None
            if trend_mode and not _trend_ok(H, L, highs, lows, cur, "long", pl):
                return None
            if mss_mode and not _mss_ok(H, L, highs, lows, cur, "long", mss_pl):
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
            ok = ccur < phj if disp_mode in ("through", "either") else ccur < float(H[j])
            if not ok:
                return None
            if trend_mode and not _trend_ok(H, L, highs, lows, cur, "short", pl):
                return None
            if mss_mode and not _mss_ok(H, L, highs, lows, cur, "short", mss_pl):
                return None
            direction = "short"
            stop = float(H[j]) + buf

    if direction is None or stop is None:
        return None

    entry = ccur                      # HONEST: the decision bar's close
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    sign = 1.0 if direction == "long" else -1.0

    if target_mode == 1:
        lo_w = max(0, cur - tgt_window + 1)
        win_h = H[lo_w:cur]
        win_l = L[lo_w:cur]
        if len(win_h) == 0:
            return None
        pool = float(np.max(win_h)) if direction == "long" else float(np.min(win_l))
        dist = sign * (pool - entry)
        if dist <= 0 or dist < min_rr * risk:
            return None
        target = pool
    else:
        target = entry + sign * rr * risk

    if sign * (stop - entry) >= 0 or sign * (target - entry) <= 0:
        return None

    return {"direction": direction, "entry": float(entry),
            "stop": float(stop), "target": float(target),
            "ts": ts_cur}
