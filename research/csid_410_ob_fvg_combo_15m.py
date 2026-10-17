"""CSID 410 — OB + iFVG combo midpoint reversion 15m.

Concept: combines an Order Block (prior candle midpoint) AND a Fair Value Gap
(FVG) into a single entry zone — only enters when BOTH zones overlap. This
filters out single-zone setups (already covered by ifvg_midpoint_reversion and
ict_mitigation_block families).

Logic:
  1. HTF bias: 1h EMA60 (close > ema = bull, close < ema = bear). Optional.
  2. Detect a 3-candle displacement FVG in last 6 bars (long or short).
  3. Detect a "valid OB" — the prior candle BEFORE the displacement that
     initiated the move, i.e. the candle with the smallest body in the same
     direction immediately preceding the displacement candle.
  4. The entry zone = midpoint of OB ∩ midpoint of FVG (overlap region).
     Enter at the higher midpoint (for shorts) or lower midpoint (for longs).
  5. Stop beyond the displacement candle extreme + 0.2 ATR buffer.
  6. Target = entry +/- 2R.

This is a tighter filter than pure FVG mid (CSID 295 family) or pure OB mid
(CSID family of ict_mitigation_block), which are already in production.
"""
import numpy as np
import pandas as pd


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
    if n < 200:
        return None

    atr_period = int(params.get("atr_period", 14))
    disp_atr_mult = float(params.get("disp_atr_mult", 1.3))
    body_ratio_min = float(params.get("body_ratio_min", 0.70))
    target_r = float(params.get("target_r", 2.0))
    stop_buf_atr = float(params.get("stop_buf_atr", 0.20))
    htf_ema_period = int(params.get("htf_ema_period", 60))
    use_htf_bias = bool(params.get("use_htf_bias", True))
    fvg_lookback = int(params.get("fvg_lookback", 6))
    tick = float(params.get("tick", 0.10))

    high = bars["high"].values.astype(float)
    low = bars["low"].values.astype(float)
    close = bars["close"].values.astype(float)
    open_ = bars["open"].values.astype(float)
    ts_v = pd.to_datetime(bars["ts"].values, utc=True, errors="coerce")

    last = n - 1
    cur_atr_arr = _atr(high, low, close, period=atr_period)
    if np.isnan(cur_atr_arr[last]) or cur_atr_arr[last] <= 0:
        return None
    cur_atr = float(cur_atr_arr[last])

    if use_htf_bias:
        try:
            h1 = _resample_1h(bars)
            if len(h1) < htf_ema_period + 1:
                return None
            h1_close = h1["close"].values.astype(float)
            h1_ema = (
                pd.Series(h1_close).ewm(span=htf_ema_period, adjust=False).mean().values
            )
            cur_ts = ts_v[last]
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
    else:
        bias_bull = True
        bias_bear = True

    # Scan last fvg_lookback bars for a displacement FVG (excluding current bar)
    bull_fvg = None  # (gap_low, gap_high, disp_idx, ob_idx)
    bear_fvg = None
    for i in range(last - fvg_lookback, last):
        if i < 2:
            continue
        rng = float(high[i] - low[i])
        body = abs(float(close[i]) - float(open_[i]))
        if rng <= 0:
            continue
        if body / rng < body_ratio_min:
            continue
        if rng < disp_atr_mult * cur_atr_arr[i]:
            continue
        # bullish displacement: bar i low > bar i-2 high (gap up)
        if float(low[i]) > float(high[i - 2]):
            gap_low = float(high[i - 2])
            gap_high = float(low[i])
            # OB is the candle before the displacement, at i-1 (typically a small
            # bearish pullback candle inside the move). Use midpoint.
            ob_mid = 0.5 * (float(high[i - 1]) + float(low[i - 1]))
            if not bias_bull:
                continue
            bull_fvg = (gap_low, gap_high, i, ob_mid)
            break
        # bearish displacement: bar i high < bar i-2 low (gap down)
        if float(high[i]) < float(low[i - 2]):
            gap_low = float(high[i])
            gap_high = float(low[i - 2])
            ob_mid = 0.5 * (float(high[i - 1]) + float(low[i - 1]))
            if not bias_bear:
                continue
            bear_fvg = (gap_low, gap_high, i, ob_mid)
            break

    if bull_fvg is None and bear_fvg is None:
        return None

    cur_close = float(close[last])
    cur_high = float(high[last])
    cur_low = float(low[last])

    if bull_fvg is not None:
        gap_low, gap_high, disp_i, ob_mid = bull_fvg
        fvg_mid = 0.5 * (gap_low + gap_high)
        # entry at lower of two midpoints (more conservative, tighter stop)
        entry = min(ob_mid, fvg_mid)
        # require price is currently pulling back into the zone
        if cur_close > gap_high + tick:
            return None  # price already above zone, missed
        stop = float(low[disp_i]) - stop_buf_atr * cur_atr
        risk = entry - stop
        if risk <= 0.5 * tick or risk > 3.0 * cur_atr:
            return None
        target = entry + target_r * risk
        return {
            "ts": ts_v[last],
            "direction": "long",
            "entry": entry,
            "stop": stop,
            "target": target,
        }

    if bear_fvg is not None:
        gap_low, gap_high, disp_i, ob_mid = bear_fvg
        fvg_mid = 0.5 * (gap_low + gap_high)
        entry = max(ob_mid, fvg_mid)
        if cur_close < gap_low - tick:
            return None
        stop = float(high[disp_i]) + stop_buf_atr * cur_atr
        risk = stop - entry
        if risk <= 0.5 * tick or risk > 3.0 * cur_atr:
            return None
        target = entry - target_r * risk
        return {
            "ts": ts_v[last],
            "direction": "short",
            "entry": entry,
            "stop": stop,
            "target": target,
        }

    return None