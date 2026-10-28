"""SWEEP -> MSS DISPLACEMENT -> FVG RETEST (market entry) -> liquidity target.

v2 of the family authored in the 2026-10-28 cycle
(research/cycle_20261028/sweep_mss_fvg_liq_target_v1.py).

WHY v2 EXISTS -- DATA HYGIENE
------------------------------
The deep bar archive is PADDED WITH PLACEHOLDER BARS wherever IBKR history
was missing:

    MGC 15m :  22,263 bars,  7,769 zero-range (35%)
    MNQ 15m :  15,069 bars,  5,676 zero-range (38%)
    MCL 15m :  19,443 bars, 10,654 zero-range (55%)
    6J  15m :  19,407 bars, 13,734 zero-range (71%)
    ZF  15m :   8,096 bars,  3,163 zero-range (39%)

A placeholder bar carries open=high=low=close at a STALE price and volume 0.
v1 had no filter, so it could manufacture a "sweep" (a wick through a level
that never happened) and a "displacement" out of padding.  EVERY number
produced by the v1 screen is therefore suspect.

v2 fixes it the way the promoted family (csid=439, market_entry_sweep_cont)
does it: the decision bar must belong to a CONTIGUOUS RUN OF REAL BARS
(range > 0 AND volume > 0), and every level / pivot / ATR is measured
INSIDE that run only.  A gap in the archive therefore cannot invent a
sweep, and a level cannot be taken across a hole in the data.

Long clean runs actually exist, so the filter costs little:
    MGC 15m: one run of 6,270 real bars (+16 runs >= 60 bars)
    MNQ 15m: 3,787 + 803 + 783 + 523 + 402 ...
    MCL 15m: 2,519 + 187 + 183 ...
    6J  15m: 2,669 + 766 ...
    ZF  15m: 735 + 481 + 461 + 438 ...

ARCHITECTURE (unchanged from v1 -- video-derived)
-------------------------------------------------
  1. LIQUIDITY SWEEP: within `sweep_age` bars, price wicks through a rolling
     pool extreme (max/min of the last `sweep_lookback` bars) by >=
     `min_sweep_atr` x ATR and CLOSES BACK INSIDE (rejection, not acceptance).
  2. MSS DISPLACEMENT: a leg with range >= `disp_k` x ATR, body ratio >=
     `body_ratio_min`, that closes through the last opposing pivot and leaves
     a 3-bar FVG.
  3. ENTRY: the FIRST tap of that FVG (`fvg_expiry` bars), MARKET at the
     decision bar's CLOSE.  No structural entry price is ever named, so the
     fill is executable (the runtime submits MARKET orders only -- finding
     #9134).  `close_in_zone=1` additionally requires the close to sit inside
     the gap.
  4. STOP: structural (beyond the sweep extreme or the FVG far edge) with an
     ATR floor (costs eat sub-0.3-point stops) and an ATR cap.
  5. TARGET: the nearest opposing liquidity pool that existed BEFORE the
     displacement leg, skipped when it is closer than `min_rr` x risk
     (Zen Trades step-4 filter: "no good liquidity, no good trade"), or a
     fixed R when target_mode == 'rr'.

The signal logic lives in `signal_at()` so the research replay and the house
engine run EXACTLY the same code path.
"""

import numpy as np

TAIL = 400

DEFAULTS = {
    "sweep_lookback": 20,     # bars forming the liquidity pool that gets swept
    "min_sweep_atr": 0.20,    # sweep depth >= x * ATR
    "sweep_age": 12,          # how many bars ago the sweep may be
    "pivot_lookback": 8,      # window for the "last opposing pivot" (MSS level)
    "disp_k": 0.60,           # displacement leg range >= x * ATR
    "body_ratio_min": 0.45,   # displacement body ratio
    "fvg_min_atr": 0.08,      # FVG height >= x * ATR
    "fvg_expiry": 10,         # bars allowed between FVG and the retest
    "stop_buf_atr": 0.10,     # buffer beyond the structural stop
    "stop_mode": "fvg",       # 'fvg' | 'sweep' | 'atr'
    "stop_atr_mult": 1.20,    # used when stop_mode == 'atr'
    "min_risk_atr": 0.80,     # floor on entry->stop distance (cost sanity)
    "mss_required": 1,        # 1 = the displacement must break the pivot
    "close_in_zone": 1,       # 1 = close must sit inside the FVG
    "atr_period": 14,
    "max_risk_atr": 3.00,     # cap on entry->stop distance at x * ATR
    "liq_lookback": 60,       # window searched for the opposing liquidity pool
    "min_rr": 1.00,           # minimum reward:risk to the liquidity pool
    "target_mode": "liquidity",   # 'liquidity' | 'rr'
    "rr": 2.0,                # used when target_mode == 'rr'
    "bias_ema": 0,            # 0 = off; else only take with the EMA trend
    "min_run_bars": 60,       # min contiguous REAL bars required at the decision
}


def _atr_series(H, L, C, period):
    """Rolling mean true range (vectorised: cumsum)."""
    n = len(C)
    if n == 0:
        return np.zeros(0, dtype=float)
    tr = np.empty(n, dtype=float)
    tr[0] = H[0] - L[0]
    if n > 1:
        pc = C[:-1]
        tr[1:] = np.maximum(H[1:] - L[1:],
                            np.maximum(np.abs(H[1:] - pc), np.abs(L[1:] - pc)))
    cs = np.cumsum(tr)
    out = np.empty(n, dtype=float)
    for i in range(n):
        s = i - period + 1
        if s <= 0:
            out[i] = cs[i] / float(i + 1)
        else:
            out[i] = (cs[i] - cs[s - 1]) / float(period)
    return out


def _ema_series(C, period):
    a = 2.0 / (period + 1.0)
    e = float(C[0])
    out = np.empty(len(C), dtype=float)
    for i in range(len(C)):
        e = a * float(C[i]) + (1.0 - a) * e
        out[i] = e
    return out


def _merged_params(params):
    p = dict(DEFAULTS)
    try:
        for _k, _v in (params or {}).items():
            if _k in DEFAULTS and _v is not None:
                p[_k] = _v
    except Exception:
        pass
    return p


def signal_at(H, L, C, O, ATR, cur, p, ema=None):
    """Return (direction, entry, stop, target) for decision bar `cur`, else None."""
    lb = int(p["sweep_lookback"])
    ms = float(p["min_sweep_atr"])
    sweep_age = int(p["sweep_age"])
    piv_lb = int(p["pivot_lookback"])
    disp_k = float(p["disp_k"])
    body_min = float(p["body_ratio_min"])
    fvg_min = float(p["fvg_min_atr"])
    fvg_exp = int(p["fvg_expiry"])
    buf = float(p["stop_buf_atr"])
    max_risk = float(p["max_risk_atr"])
    liq_lb = int(p["liq_lookback"])
    min_rr = float(p["min_rr"])
    stop_mode = str(p["stop_mode"]).lower()
    close_in_zone = int(p["close_in_zone"])
    target_mode = str(p["target_mode"]).lower()
    rr_fixed = float(p["rr"])
    stop_atr_mult = float(p["stop_atr_mult"])
    min_risk = float(p["min_risk_atr"])
    mss_required = int(p["mss_required"])

    a_cur = float(ATR[cur])
    if not np.isfinite(a_cur) or a_cur <= 0:
        return None

    # ---- 1. most recent liquidity sweep (wick through + close back inside)
    sweep_k = -1
    direction = None
    pool_hi = 0.0
    pool_lo = 0.0
    sweep_ext = 0.0
    for k in range(cur - 1, max(0, cur - sweep_age - 1), -1):
        if k - lb < 2:
            break
        a_k = float(ATR[k])
        if not np.isfinite(a_k) or a_k <= 0:
            continue
        ph = float(np.max(H[k - lb:k]))
        pl = float(np.min(L[k - lb:k]))
        if L[k] < pl - ms * a_k and C[k] > pl:
            sweep_k, direction = k, "long"
            pool_hi, pool_lo, sweep_ext = ph, pl, float(L[k])
            break
        if H[k] > ph + ms * a_k and C[k] < ph:
            sweep_k, direction = k, "short"
            pool_hi, pool_lo, sweep_ext = ph, pl, float(H[k])
            break
    if sweep_k < 0:
        return None

    # ---- 2. the last opposing pivot that the displacement must break
    if direction == "long":
        piv = float(np.max(H[max(0, sweep_k - piv_lb):sweep_k + 1]))
    else:
        piv = float(np.min(L[max(0, sweep_k - piv_lb):sweep_k + 1]))

    # ---- 3. displacement leg that breaks the pivot AND leaves an FVG;
    #         then the retest into that FVG (the FIRST tap after the FVG)
    entry = float(C[cur])
    zone_bot = zone_top = 0.0
    leg_k = -1
    for m in range(cur, sweep_k, -1):
        if m - 2 <= sweep_k:
            break
        rng = float(H[m] - L[m])
        if rng <= 0:
            continue
        br = abs(float(C[m] - O[m])) / rng
        if rng < disp_k * float(ATR[m]) or br < body_min:
            continue
        if direction == "long":
            if mss_required and float(C[m]) <= piv:
                continue
            bot = float(H[m - 2])
            top = float(L[m])
            if not (top > bot) or (top - bot) < fvg_min * float(ATR[m]):
                continue
            tap = -1
            for j in range(m + 1, min(cur, m + fvg_exp) + 1):
                if float(L[j]) <= top and float(H[j]) >= bot:
                    tap = j
                    break
            if tap != cur:
                continue
            if close_in_zone:
                if not (bot <= entry <= top):
                    continue
            elif entry > top:
                continue
            zone_bot, zone_top, leg_k = bot, top, m
            break
        else:
            if float(C[m]) >= piv:
                continue
            bot = float(L[m - 2])
            top = float(H[m])
            if not (top > bot) or (top - bot) < fvg_min * float(ATR[m]):
                continue
            tap = -1
            for j in range(m + 1, min(cur, m + fvg_exp) + 1):
                if float(H[j]) >= bot and float(L[j]) <= top:
                    tap = j
                    break
            if tap != cur:
                continue
            if close_in_zone:
                if not (bot <= entry <= top):
                    continue
            elif entry < bot:
                continue
            zone_bot, zone_top, leg_k = bot, top, m
            break
    if leg_k < 0:
        return None

    # ---- 4. stop: structural by default, ATR-widened for cost sanity
    if stop_mode == "atr":
        stop = (entry - stop_atr_mult * a_cur if direction == "long"
                else entry + stop_atr_mult * a_cur)
    else:
        base = sweep_ext if stop_mode == "sweep" else (zone_bot if direction == "long" else zone_top)
        stop = base - buf * a_cur if direction == "long" else base + buf * a_cur
        wrong_side = (stop >= entry) if direction == "long" else (stop <= entry)
        if wrong_side:
            stop = (entry - stop_atr_mult * a_cur if direction == "long"
                    else entry + stop_atr_mult * a_cur)
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    if min_risk > 0 and risk < min_risk * a_cur:
        stop = (entry - min_risk * a_cur if direction == "long"
                else entry + min_risk * a_cur)
        risk = min_risk * a_cur
    if risk > max_risk * a_cur:
        return None

    # ---- 5. optional HTF bias filter
    bias_ema = int(p["bias_ema"])
    if bias_ema > 2:
        if ema is None:
            ema = _ema_series(C, bias_ema)
        if direction == "long" and float(C[cur]) <= float(ema[cur]):
            return None
        if direction == "short" and float(C[cur]) >= float(ema[cur]):
            return None

    # ---- 6. target
    if target_mode == "rr":
        target = entry + rr_fixed * risk * (1.0 if direction == "long" else -1.0)
    else:
        w0 = max(0, leg_k - liq_lb)
        win_h = H[w0:leg_k]
        win_l = L[w0:leg_k]
        if direction == "long":
            cand = win_h[win_h > entry + min_rr * risk]
            if cand.size == 0:
                return None
            target = float(np.min(cand))
        else:
            cand = win_l[win_l < entry - min_rr * risk]
            if cand.size == 0:
                return None
            target = float(np.max(cand))

    sign = 1.0 if direction == "long" else -1.0
    if sign * (stop - entry) >= 0 or sign * (target - entry) <= 0:
        return None
    return direction, entry, float(stop), float(target)


def decide(H, L, C, O, V, p):
    """DATA-HYGIENE GATE + signal.

    The decision bar must be part of a CONTIGUOUS RUN OF REAL BARS
    (high > low and volume > 0).  Every level, pivot and ATR is measured
    inside that run only, so a placeholder bar (or a hole in the archive)
    can never manufacture a sweep or a displacement.
    """
    n = len(C)
    if n < 40:
        return None
    if V is None:
        real = (H > L)
    else:
        real = (H > L) & (V > 0)
    if not bool(real[n - 1]):
        return None
    s = n - 1
    while s > 0 and bool(real[s - 1]):
        s -= 1
    if (n - s) < int(p.get("min_run_bars", 60)):
        return None
    Hr, Lr, Cr, Or = H[s:], L[s:], C[s:], O[s:]
    atr = _atr_series(Hr, Lr, Cr, int(p["atr_period"]))
    ema = _ema_series(Cr, int(p["bias_ema"])) if int(p["bias_ema"]) > 2 else None
    return signal_at(Hr, Lr, Cr, Or, atr, len(Cr) - 1, p, ema)


def evaluate(bars, params):
    """Engine entry point: decide on the LAST bar of `bars`."""
    p = _merged_params(params)
    if bars is None or len(bars) < 40:
        return None
    b = bars.tail(TAIL) if len(bars) > TAIL else bars
    H = b["high"].to_numpy(float)
    L = b["low"].to_numpy(float)
    C = b["close"].to_numpy(float)
    O = b["open"].to_numpy(float)
    V = b["volume"].to_numpy(float) if "volume" in b.columns else None
    sig = decide(H, L, C, O, V, p)
    if sig is None:
        return None
    direction, entry, stop, target = sig
    return {
        "direction": direction,
        "entry": entry,
        "stop": stop,
        "target": target,
        "ts": b["ts"].iloc[-1],
    }
