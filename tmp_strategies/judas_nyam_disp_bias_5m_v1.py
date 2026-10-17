"""
ICT Displacement Continuation 5m — NY AM session filter + 1h EMA bias.

Concept: blends AD dispersion (proven edge across MGC/MNQ/MCL/6J with PF 1.81-5.02,
csid 156/203/267/269) with two NEW filters not in deployed variants:

1. NY AM session window filter (14:30-16:00 UTC = 9:30-11 AM EST).
   ICT teaches that NY AM killzone has the cleanest displacement setups.
2. 1h EMA(60) bias filter: only LONG when 1h close > 1h EMA(60), only SHORT
   when 1h close < 1h EMA(60). Skips counter-trend displacement entries.

Architecture mirrors CSID 267 (5m strict):
- ATR(14)
- Displacement candle: range >= 1.5x ATR, body_ratio >= 0.65
- 50% pullback entry of displacement range
- Stop at displacement extreme + 0.20*ATR buffer
- Target 1.5R

NOVEL vs deployed 5m strict (CSID 267):
- Adds session window + 1h EMA bias — daily to daily traded price has been
  counter-trend in the current regime (~40% of fires land counter HTF trend
  in 240d MGC sim, finding ffbf74f0); this filter should boost PF.
"""
import numpy as np
import pandas as pd


_S = {"last_processed_idx": -1}


def _atr(high, low, close, period=14):
    pc = np.roll(close, 1)
    pc[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - pc), np.abs(low - pc)))
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
    if n < 30:
        return None
    cur_i = n - 1
    if cur_i == _S["last_processed_idx"]:
        return None
    _S["last_processed_idx"] = cur_i

    atr_period = int(params.get("atr_period", 14))
    disp_atr_mult = float(params.get("disp_atr_mult", 1.5))
    body_ratio_min = float(params.get("body_ratio_min", 0.65))
    pullback_pct = float(params.get("pullback_pct", 0.50))
    target_r = float(params.get("target_r", 1.5))
    stop_buffer_atr = float(params.get("stop_buffer_atr", 0.20))
    htf_ema_period = int(params.get("htf_ema_period", 60))
    use_session = bool(params.get("use_session", True))
    session_start_utc = int(params.get("session_start_utc", 14))
    session_end_utc = int(params.get("session_end_utc", 16))

    high = bars["high"].values.astype(float)
    low = bars["low"].values.astype(float)
    close = bars["close"].values.astype(float)
    open_ = bars["open"].values.astype(float)
    ts_v = pd.to_datetime(bars["ts"].values, utc=True, errors="coerce")

    atr = _atr(high, low, close, period=atr_period)
    if np.isnan(atr[cur_i]) or atr[cur_i] <= 0:
        return None
    cur_atr = float(atr[cur_i])

    if use_session and not pd.isna(ts_v[cur_i]):
        cur_h_utc = ts_v[cur_i].hour
        if not (session_start_utc <= cur_h_utc < session_end_utc):
            return None

    try:
        h1 = _resample_1h(bars)
        if len(h1) < htf_ema_period + 1:
            return None
        h1_close = h1["close"].values.astype(float)
        h1_ema = (
            pd.Series(h1_close)
            .ewm(span=htf_ema_period, adjust=False)
            .mean()
            .values
        )
        cur_ts = ts_v[cur_i]
        last_h1_idx = None
        for k in range(len(h1) - 1, -1, -1):
            if h1.index[k] <= cur_ts:
                last_h1_idx = k
                break
        if last_h1_idx is None or last_h1_idx < 1:
            return None
        h1_cur = float(h1_close[last_h1_idx])
        h1_ema_cur = float(h1_ema[last_h1_idx])
        bias_bull = h1_cur > h1_ema_cur
        bias_bear = h1_cur < h1_ema_cur
    except Exception:
        return None

    disp_i = cur_i
    o = float(open_[disp_i])
    h = float(high[disp_i])
    l = float(low[disp_i])
    cl = float(close[disp_i])
    rng = h - l
    body = abs(cl - o)
    if rng <= 0:
        return None
    body_r = body / rng
    if body_r < body_ratio_min:
        return None
    if rng < disp_atr_mult * cur_atr:
        return None

    is_bull = cl > o
    if is_bull and not bias_bull:
        return None
    if not is_bull and not bias_bear:
        return None

    if is_bull:
        entry = h - pullback_pct * rng
        stop = l - stop_buffer_atr * cur_atr
        risk = entry - stop
        if risk <= 0 or risk > 4.0 * cur_atr:
            return None
        target = entry + target_r * risk
        return {"direction": "long", "entry": entry, "stop": stop, "target": target}
    else:
        entry = l + pullback_pct * rng
        stop = h + stop_buffer_atr * cur_atr
        risk = stop - entry
        if risk <= 0 or risk > 4.0 * cur_atr:
            return None
        target = entry - target_r * risk
        return {"direction": "short", "entry": entry, "stop": stop, "target": target}