"""MARKET-ENTRY SWEEP -> DISPLACEMENT CONTINUATION (v2) -- fair-fill safe.

WHY THIS FILE EXISTS (2026-10-30, finding 9164 + commit 7c0106c):
  The house custom backtest engine used to resolve every entry on a bar the
  strategy had ALREADY closed (retro-fill).  Under the corrected
  PRODUCTION model (entry decided on the close of bar T fills at the OPEN of
  bar T+1) the whole legacy library collapses (PF 4-6 -> PF 0.1-0.5):
  silver_bullet_pdh_pdl_retest_*, ict_mitigation_block_*,
  ifvg_midpoint_reversion_*, htf_1h_disp_5m_fvg_pullback -- all of them were
  fill artefacts.  The only two custom strategies in the book that SURVIVE the
  corrected engine are the market-entry family (csid439 MGC 15m, csid441 MGC
  5m).  This file is that family, kept architecturally identical, with a
  handful of NEW optional knobs so the same code can be swept across
  symbols/timeframes and filtered without rewriting it each cycle.

ARCHITECTURE (unchanged core -- it is the part that survived):
  1. SWEEP: a bar k within the last `confirm_bars` takes out a rolling
     `lookback` high/low by >= min_sweep_atr * ATR and closes back inside
     the range (liquidity grab);
  2. DISPLACEMENT: the decision bar closes back through the swept level with
     body ratio >= body_ratio_min and range >= disp_k * ATR;
  3. entry = decision close (the price the broker can actually pay -- the
     live path submits a MARKET order, there is no LIMIT entry path here);
     stop beyond the sweep extreme + buffer; target at rr * risk.

NEW OPTIONAL KNOBS (all default to OFF -> bit-identical to csid439):
  htf_bias_bars   : if > 0, resample the series to `htf_bias_bars` x the
                    base timeframe and require the trend (close vs EMA) to
                    agree with the trade direction.
  htf_ema         : EMA length on the resampled series (default 20).
  session_start_h / session_end_h : UTC hour window in which a decision is
                    allowed (None -> no filter).
  long_only / short_only : direction filters.
  min_rr_atr      : if > 0, require the distance from entry to the structural
                    stop to be at least this many ATR (quality filter on the
                    structural stop distance).
  max_rr_atr      : if > 0, reject trades whose structural risk is wider
                    than this many ATR.
  sweep_mode      : "rolling" (csid439) | "swing" (pivot based).
  pivot_len       : swing pivot lookback when sweep_mode == "swing".
  require_fvg     : if True, the displacement bar must leave a 3-bar FVG
                    (gap between bar i-2 high and bar i low, or vice versa).
  fvg_min_atr     : minimum FVG height in ATR when require_fvg is on.
"""
import numpy as np

TAIL = 400
DEFAULTS = {
    "lookback": 12,
    "min_sweep_atr": 0.3,
    "disp_k": 1.0,
    "rr": 1.5,
    "confirm_bars": 6,
    "body_ratio_min": 0.55,
    "stop_buf_atr": 0.15,
    "atr_period": 14,
    # --- new knobs (defaults = OFF, bit-identical to csid439) -----------
    "htf_bias_bars": 0,
    "htf_ema": 20,
    "trend_bars": 0,          # >0 -> require close[cur] vs close[cur-trend_bars]
    "trend_dir": 1,           # +1: momentum up favours longs
    "session_start_h": -1,
    "session_end_h": -1,
    "long_only": 0,
    "short_only": 0,
    "min_rr_atr": 0.0,
    "max_rr_atr": 0.0,
    "sweep_mode": "rolling",
    "pivot_len": 5,
    "require_fvg": 0,
    "fvg_min_atr": 0.0,
    # --- liquidity-target exit (YT:dSuYxfhf1Ko: "target the next liquidity") -
    "target_mode": "rr",       # "rr" (fixed R) | "liq" (next liquidity level)
    "liq_lookback": 96,        # bars back for the liquidity extreme
    "liq_min_rr": 1.0,         # liquidity must be at least this many R away
    "liq_max_rr": 4.0,         # cap the liquidity target at this many R
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


def _ema(x, period):
    if len(x) < 2:
        return x
    period = int(max(1, period))
    alpha = 2.0 / (period + 1.0)
    out = np.empty(len(x), dtype=float)
    out[0] = float(x[0])
    for i in range(1, len(x)):
        out[i] = alpha * float(x[i]) + (1.0 - alpha) * out[i - 1]
    return out


def _htf_bias(bars, mult, ema_len, sign):
    """Trend read on a RESAMPLED (higher-timeframe) close series.

    sign=+1 -> require an uptrend on the higher timeframe.
    Returns None if the read cannot be taken (the caller then rejects).
    """
    try:
        import pandas as pd
    except Exception:
        return None
    try:
        ts = bars["ts"]
        s = pd.Series(np.asarray(bars["close"], dtype=float),
                      index=pd.DatetimeIndex(ts))
        deltas = s.index.to_series().diff().dropna()
        if len(deltas) == 0:
            return None
        base_min = max(1, int(deltas.median().total_seconds() // 60))
        rule = f"{max(1, int(mult)) * base_min}min"
        htf = s.resample(rule).last().dropna()
        if len(htf) < 3:
            return None
        vals = htf.to_numpy(float)
        e = _ema(vals, int(ema_len))
        if not np.isfinite(e[-1]):
            return None
        return (float(vals[-1]) - float(e[-1])) * sign > 0.0
    except Exception:
        return None


def evaluate(bars, params):
    p = dict(DEFAULTS)
    try:
        p.update({k: v for k, v in (params or {}).items()
                  if k in DEFAULTS and v is not None})
    except Exception:
        pass

    lb = int(p["lookback"])
    confirm = int(p["confirm_bars"])
    ms = float(p["min_sweep_atr"])
    disp_k = float(p["disp_k"])
    rr = float(p["rr"])
    min_body = float(p["body_ratio_min"])
    buf = float(p["stop_buf_atr"])
    atr_period = int(p["atr_period"])
    pivot_len = int(p["pivot_len"])
    sweep_mode = str(p["sweep_mode"])
    require_fvg = bool(p["require_fvg"])
    fvg_min_atr = float(p["fvg_min_atr"])
    min_rr_atr = float(p["min_rr_atr"])
    max_rr_atr = float(p["max_rr_atr"])

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

    # ---- optional session filter (UTC hour of the decision bar) --------
    sh = int(p["session_start_h"])
    eh = int(p["session_end_h"])
    if sh >= 0 and eh >= 0:
        try:
            hour = pd_hour(bars, cur)
        except Exception:
            hour = None
        if hour is not None:
            if sh <= eh:
                if not (sh <= hour < eh):
                    return None
            else:  # wraps midnight
                if not (hour >= sh or hour < eh):
                    return None

    direction = None
    stop = None

    j = -1
    for k in range(cur - 1, max(0, cur - confirm - 1), -1):
        if k < lb + 1:
            continue
        if sweep_mode == "swing":
            phk = float(np.max(H[max(0, k - pivot_len):k])) if k > 0 else H[k]
            plk = float(np.min(L[max(0, k - pivot_len):k])) if k > 0 else L[k]
        else:
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

    if sweep_mode == "swing":
        plj = float(np.min(L[max(0, j - pivot_len):j])) if j > 0 else L[j]
        phj = float(np.max(H[max(0, j - pivot_len):j])) if j > 0 else H[j]
    else:
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

    if require_fvg:
        # 3-bar FVG left by the displacement bar (bar i-2 vs bar i)
        i = cur
        if i < 2:
            return None
        bull = float(L[i]) > float(H[i - 2])
        bear = float(H[i]) < float(L[i - 2])
        if direction == "long" and not bull:
            return None
        if direction == "short" and not bear:
            return None
        gap = (float(L[i]) - float(H[i - 2])) if bull else (float(L[i - 2]) - float(H[i]))
        if fvg_min_atr > 0 and gap < fvg_min_atr * a:
            return None

    if bool(p["long_only"]) and direction != "long":
        return None
    if bool(p["short_only"]) and direction != "short":
        return None

    # ---- optional momentum agreement (close vs close N bars ago) -------
    tb = int(p["trend_bars"])
    if tb > 1 and cur - tb > 0:
        ref_c = float(C[cur - tb])
        mom = (ccur - ref_c) * sign
        if float(p["trend_dir"]) > 0:
            if mom <= 0:
                return None
        else:
            if mom >= 0:
                return None

    entry = float(ccur)
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    if min_rr_atr > 0 and risk < min_rr_atr * a:
        return None
    if max_rr_atr > 0 and risk > max_rr_atr * a:
        return None

    sign = 1.0 if direction == "long" else -1.0

    # ---- optional HTF trend agreement ----------------------------------
    hb = int(p["htf_bias_bars"])
    if hb > 1:
        ok = _htf_bias(bars, hb, int(p["htf_ema"]), sign)
        if ok is None or not ok:
            return None

    target = entry + sign * rr * risk

    # ---- LIQUIDITY-TARGET EXIT -----------------------------------------
    # "Target the next liquidity" (YT:dSuYxfhf1Ko, 2026-10-30): instead of a
    # fixed R multiple, aim at the next liquidity pool -- for a long the
    # rolling swing high above, for a short the rolling swing low below --
    # but never further than liq_max_rr R, and only if the pool is at least
    # liq_min_rr R away (otherwise the trade has no room).
    if str(p["target_mode"]) == "liq":
        nliq = int(p["liq_lookback"])
        if nliq > 3 and cur - nliq > 0:
            if direction == "long":
                liq = float(np.max(H[max(0, cur - nliq):cur]))
            else:
                liq = float(np.min(L[max(0, cur - nliq):cur]))
            dist = (liq - entry) * sign
            if dist >= float(p["liq_min_rr"]) * risk:
                capped = entry + sign * float(p["liq_max_rr"]) * risk
                target = min(liq, capped) if direction == "long" else max(liq, capped)
                # never let the liquidity target sit worse than the fixed R
                if sign * (target - entry) <= 0:
                    return None
    if sign * (stop - entry) >= 0 or sign * (target - entry) <= 0:
        return None

    return {"direction": direction, "entry": entry,
            "stop": float(stop), "target": float(target),
            "ts": bars["ts"].iloc[cur]}


def pd_hour(bars, cur):
    ts = bars["ts"].iloc[cur]
    try:
        return int(ts.hour)
    except Exception:
        return None
