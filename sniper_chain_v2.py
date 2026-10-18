"""
SNIPER CHAIN v2 — sweep + MSS + displacement + FVG midpoint entry (Lux IQ style).

Architecture:
  1. Identify a swing liquidity pool (swing high/low) over lookback bars.
  2. Wait for a sweep: price wicks beyond the pool then closes back inside.
  3. Confirm MSS: close > swing high (long) or close < swing low (short).
  4. Wait for a displacement candle: body_ratio >= min_body AND range >= min_disp_atr*ATR.
  5. Locate the 3-candle FVG (gap) created by the displacement.
  6. Entry at FVG midpoint on retest within fvg_expiry bars.
  7. Stop = beyond sweep wick +/- stop_buf*ATR.
  8. Target = entry + target_r * risk.

Filters:
  - NY AM session (13-16 UTC) for maximum displacement reliability.
  - 1h EMA bias filter (htf_ema_period vs close).

Distinction from existing live families:
  - silver_bullet_pdh_pdl_retest: rolling PDH/PDL pool, no MSS/disp/FVG requirements.
  - ict_mitigation_block: candle before displacement, no FVG midpoint entry.
  - ifvg_midpoint_reversion: any iFVG, no sweep+MSS pre-qualification.
  - judas_nyam_disp_bias: displacement continuation, no FVG midpoint.

Backtest intent (90d, 5m native): MGC, MNQ, MCL, ZF, 6J.
"""
import numpy as np
import pandas as pd


def _atr(high, low, close, period=14):
    pc = np.concatenate([[close[0]], close[:-1]])
    tr = np.maximum(high - low,
                    np.maximum(np.abs(high - pc), np.abs(low - pc)))
    return pd.Series(tr).ewm(alpha=1.0 / period, adjust=False).mean().values


def _resample_1h(bars):
    df = bars[["ts", "open", "high", "low", "close"]].copy()
    df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    df = df.dropna(subset=["ts"]).set_index("ts")
    h1 = df.resample("1h").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).dropna()
    return h1


def evaluate(bars, params):
    n = len(bars)
    if n < 60:
        return None
    cur_i = n - 1

    lookback = int(params.get("lookback", 30))
    sweep_lookback = int(params.get("sweep_lookback", 10))
    disp_lookback = int(params.get("disp_lookback", 6))
    fvg_expiry = int(params.get("fvg_expiry", 12))
    min_body = float(params.get("min_body", 0.65))
    min_disp_atr = float(params.get("min_disp_atr", 1.2))
    atr_period = int(params.get("atr_period", 14))
    target_r = float(params.get("target_r", 2.0))
    stop_buf = float(params.get("stop_buf", 0.20))
    htf_ema_period = int(params.get("htf_ema_period", 60))
    use_session = bool(params.get("use_session", True))
    sess_start = int(params.get("session_start_utc", 13))
    sess_end = int(params.get("session_end_utc", 16))

    high = bars["high"].values.astype(float)
    low = bars["low"].values.astype(float)
    close = bars["close"].values.astype(float)
    open_ = bars["open"].values.astype(float)
    ts_v = pd.to_datetime(bars["ts"].values, utc=True, errors="coerce")

    atr = _atr(high, low, close, period=atr_period)
    cur_atr = float(atr[cur_i])
    if cur_atr <= 0 or np.isnan(cur_atr):
        return None

    if use_session:
        cur_ts = ts_v[cur_i]
        if pd.isna(cur_ts):
            return None
        hr = int(cur_ts.hour)
        if not (sess_start <= hr < sess_end):
            return None

    try:
        h1 = _resample_1h(bars)
        if len(h1) < htf_ema_period + 1:
            return None
        h1_close = h1["close"].values.astype(float)
        h1_ema = pd.Series(h1_close).ewm(span=htf_ema_period, adjust=False).mean().values
        cur_ts = ts_v[cur_i]
        last_h1_idx = None
        for k in range(len(h1) - 1, -1, -1):
            if h1.index[k] <= cur_ts:
                last_h1_idx = k
                break
        if last_h1_idx is None or last_h1_idx < 1:
            return None
        bias_bull = float(h1_close[last_h1_idx]) > float(h1_ema[last_h1_idx])
        bias_bear = float(h1_close[last_h1_idx]) < float(h1_ema[last_h1_idx])
    except Exception:
        return None

    swing_h = float(np.max(high[max(0, cur_i - lookback):cur_i - sweep_lookback])) if cur_i - sweep_lookback > 0 else float("-inf")
    swing_l = float(np.min(low[max(0, cur_i - lookback):cur_i - sweep_lookback])) if cur_i - sweep_lookback > 0 else float("inf")
    if swing_h == float("-inf") or swing_l == float("inf"):
        return None

    swept_high = False
    swept_low = False
    sweep_wick_high = float("-inf")
    sweep_wick_low = float("inf")
    sweep_idx = -1
    mss_idx = -1
    mss_bull = False
    mss_bear = False

    for j in range(cur_i - sweep_lookback, cur_i):
        if high[j] > swing_h and close[j] < swing_h:
            swept_high = True
            if high[j] > sweep_wick_high:
                sweep_wick_high = high[j]
            if sweep_idx < 0:
                sweep_idx = j
        if low[j] < swing_l and close[j] > swing_l:
            swept_low = True
            if low[j] < sweep_wick_low:
                sweep_wick_low = low[j]
            if sweep_idx < 0:
                sweep_idx = j

    if not swept_high and not swept_low:
        return None
    if swept_high and not bias_bull:
        return None
    if swept_low and not bias_bear:
        return None

    for k in range(max(sweep_idx + 1, 0), cur_i + 1):
        if close[k] > swing_h:
            mss_bull = True
            mss_idx = k
            break
        if close[k] < swing_l:
            mss_bear = True
            mss_idx = k
            break
    if mss_idx < 0:
        return None
    if mss_bull and not bias_bull:
        return None
    if mss_bear and not bias_bear:
        return None

    direction = None
    disp_idx = -1
    for d in range(mss_idx + 1, min(cur_i + 1, mss_idx + disp_lookback + 1)):
        rng = high[d] - low[d]
        if rng <= 0:
            continue
        body = abs(close[d] - open_[d])
        if body / rng < min_body:
            continue
        if rng < min_disp_atr * atr[d]:
            continue
        if close[d] > open_[d] and mss_bull:
            direction = "long"
            disp_idx = d
            break
        if close[d] < open_[d] and mss_bear:
            direction = "short"
            disp_idx = d
            break
    if direction is None:
        return None

    fvg_top = None
    fvg_bot = None
    for f in range(max(disp_idx - 1, 1), disp_idx + 1):
        if direction == "long" and f >= 2:
            c3h = high[f - 2]
            c1l = low[f]
            if c1l > c3h:
                fvg_top = c1l
                fvg_bot = c3h
                break
        if direction == "short" and f >= 2:
            c3l = low[f - 2]
            c1h = high[f]
            if c3l > c1h:
                fvg_top = c3l
                fvg_bot = c1h
                break
    if fvg_top is None:
        return None

    age_limit = disp_idx + fvg_expiry
    if age_limit < cur_i:
        return None

    entry = 0.5 * (fvg_top + fvg_bot)
    if direction == "long":
        if low[cur_i] > fvg_top or close[cur_i] < fvg_bot:
            return None
        if high[cur_i] < fvg_bot:
            return None
        sweep_wick = sweep_wick_low
        stop = min(sweep_wick, fvg_bot) - stop_buf * cur_atr
        risk = entry - stop
        if risk <= 0 or risk > 4.0 * cur_atr:
            return None
        target = entry + target_r * risk
    else:
        if high[cur_i] < fvg_bot or close[cur_i] > fvg_top:
            return None
        if low[cur_i] > fvg_top:
            return None
        sweep_wick = sweep_wick_high
        stop = max(sweep_wick, fvg_top) + stop_buf * cur_atr
        risk = stop - entry
        if risk <= 0 or risk > 4.0 * cur_atr:
            return None
        target = entry - target_r * risk

    return {
        "direction": direction,
        "entry": float(entry),
        "stop": float(stop),
        "target": float(target),
    }