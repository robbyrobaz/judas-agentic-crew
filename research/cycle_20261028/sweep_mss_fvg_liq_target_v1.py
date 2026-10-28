"""
SWEEP -> MSS DISPLACEMENT -> FVG RETEST, structural liquidity target.
sweep_mss_fvg_liq_target_v1  (Zen Trades + TradeIQ liquidity-sweep model)

RULES INGESTED 2026-10-28
-------------------------
TradeIQ "The Liquidity Sweep: How Banks Steal Your Stop Loss"
(video l27wF-EvDkw, 2026-10-28):
  1. mark the obvious liquidity (equal lows/highs, swing points, prior-day
     extremes, session extremes);
  2. a SWEEP is a wick THROUGH the level with a CLOSE BACK INSIDE
     (rejection, not acceptance);
  3. wait for the SHIFT: displacement that breaks the last opposing pivot
     (market structure shift) and leaves a fair value gap;
  4. enter on the RETEST of the FVG; stop beyond the sweep extreme;
     target = the liquidity pool on the other side;
  5. filters: trade with the higher timeframe; no displacement, no trade.

Zen Trades "The Easiest ICT Liquidity Sweep Strategy in 2026"
(video pmexdsocN9c, 2026-10-28):
  3. entry trigger is an INVERSION of the FVG (close through the gap);
  4. stop goes beyond the swing that swept the liquidity;
  5. STEP-4 FILTER: only take the trade if there is real opposing liquidity
     to target -- "if there's no good liquidity, there's no good trade".

ENGINEERING CHOICES (evidence-driven; findings 07f3a4c2 / 9134 / 9145)
------------------------------------------------------------------------
* Entry = the DECISION BAR'S CLOSE (a market order).  The live runtime
  submits a MARKET order; there is no limit path in this repo, so a limit
  entry at the FVG midpoint is a retro-fill artefact.  We pay the price the
  broker can actually pay.
* Target = the nearest opposing liquidity pool that existed BEFORE the
  displacement leg (the "map" drawn on the higher timeframe), not a fixed R.
  If that pool sits closer than min_rr x risk the trade is SKIPPED -- the
  Zen step-4 filter, coded.
* Stop = structural: `stop_mode='fvg'` puts it beyond the imbalance that is
  being mitigated (the FVG far edge); `stop_mode='sweep'` puts it beyond the
  sweep extreme, as the videos describe.
* Geometry is one-sided: the stop must sit on the losing side of the entry
  and the target on the winning side (finding 9145's inverted comparison kept
  only the broken trades).

The signal logic lives in `signal_at()` so the research replay
(research/cycle_20261028/replay.py) and the live engine run EXACTLY the same
code path -- there is no second implementation to drift.
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
    "max_risk_atr": 3.00,     # cap entry->stop distance at x * ATR
    "liq_lookback": 60,       # window searched for the opposing liquidity pool
    "min_rr": 1.00,           # minimum reward:risk to the liquidity pool
    "target_mode": "liquidity",   # 'liquidity' | 'rr'
    "rr": 2.0,                # used when target_mode == 'rr'
    "bias_ema": 0,            # 0 = off; else only take with the EMA trend
}


def _atr_series(H, L, C, period):
    n = len(C)
    tr = np.empty(n, dtype=float)
    tr[0] = H[0] - L[0]
    if n > 1:
        pc = C[:-1]
        tr[1:] = np.maximum(H[1:] - L[1:],
                            np.maximum(np.abs(H[1:] - pc), np.abs(L[1:] - pc)))
    out = np.empty(n, dtype=float)
    run = 0.0
    for i in range(n):
        run += tr[i]
        if i >= period:
            run -= tr[i - period]
        out[i] = run / float(min(i + 1, period))
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
    #      (v1_realistic_micros charges ~$2.30-4.30 per MGC trade, so a
    #       sub-0.3-point stop is eaten by costs -- the floor below keeps
    #       the trade economics honest)
    if stop_mode == "atr":
        stop = entry - stop_atr_mult * a_cur if direction == "long" else entry + stop_atr_mult * a_cur
    else:
        base = sweep_ext if stop_mode == "sweep" else (zone_bot if direction == "long" else zone_top)
        stop = base - buf * a_cur if direction == "long" else base + buf * a_cur
        wrong_side = (stop >= entry) if direction == "long" else (stop <= entry)
        if wrong_side:
            stop = entry - stop_atr_mult * a_cur if direction == "long" else entry + stop_atr_mult * a_cur
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    if min_risk > 0 and risk < min_risk * a_cur:
        stop = entry - min_risk * a_cur if direction == "long" else entry + min_risk * a_cur
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


def evaluate(bars, params):
    """Engine entry point: decide on the LAST bar of `bars`."""
    p = _merged_params(params)
    if bars is None or len(bars) < 140:
        return None
    bars = bars.tail(TAIL) if len(bars) > TAIL else bars
    n = len(bars)
    cur = n - 1
    H = bars["high"].to_numpy(float)
    L = bars["low"].to_numpy(float)
    C = bars["close"].to_numpy(float)
    O = bars["open"].to_numpy(float)
    ATR = _atr_series(H, L, C, int(p["atr_period"]))
    ema = _ema_series(C, int(p["bias_ema"])) if int(p["bias_ema"]) > 2 else None
    sig = signal_at(H, L, C, O, ATR, cur, p, ema)
    if sig is None:
        return None
    direction, entry, stop, target = sig
    return {
        "direction": direction,
        "entry": entry,
        "stop": stop,
        "target": target,
        "ts": bars["ts"].iloc[cur],
    }
