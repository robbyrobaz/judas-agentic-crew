"""
OB Midpoint Reversion + 1h EMA Bias (5m)
Based on Ranjit's 121-trade test finding (YT:3ovKpmGXa8A):
  - OB midpoint reversion alone is 50/50 (coin flip at 1-2R)
  - HTF bias is what makes the difference: "Higher time frame decides whether to act"

Architecture (csid 250 base + HTF bias filter):
  - 1h EMA(60) bias:
      Long entry only if 1h close > 1h EMA60
      Short entry only if 1h close < 1h EMA60
  - Displacement candle (>= displacement_k * ATR, body_ratio >= body_ratio_min)
  - Bullish OB: prior red candle + next green displacement that closes above prior swing high
  - Bearish OB: prior green candle + next red displacement that closes below prior swing low
  - Entry: midpoint of OB zone (open to low for longs, open to high for shorts)
  - Stop: OB edge +/- ATR buffer
  - Target: 2R
  - Skip if OB already mitigated (cur_low/cur_high broke through)
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
    df = bars[["ts", "open", "high", "low", "close", "volume"]].copy()
    df["ts"] = pd.to_datetime(df["ts"])
    df = df.set_index("ts")
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    out = df.resample("1h").agg(agg).dropna().reset_index()
    return out


def _ema(series, period):
    return pd.Series(series).ewm(span=period, adjust=False).mean().values


def evaluate(bars, params):
    n = len(bars)
    if n < 60:
        return None
    cur_i = n - 1
    if cur_i < 30:
        return None
    if cur_i == _S["last_processed_idx"]:
        return None
    _S["last_processed_idx"] = cur_i

    atr_period = int(params.get("atr_period", 14))
    displacement_k = float(params.get("displacement_k", 1.0))
    rr = float(params.get("rr", 2.0))
    stop_buf_atr = float(params.get("stop_buf_atr", 0.20))
    ob_age_max = int(params.get("ob_age_max", 24))
    body_ratio_min = float(params.get("body_ratio_min", 0.5))
    htf_ema_period = int(params.get("htf_ema_period", 60))

    rngs = (bars["high"].iloc[cur_i - atr_period:cur_i].values.astype(float) -
            bars["low"].iloc[cur_i - atr_period:cur_i].values.astype(float))
    atr = float(np.mean(rngs))
    if atr <= 0:
        return None
    buf = atr * stop_buf_atr

    # HTF bias: 1h EMA direction
    h1 = _resample_1h(bars.iloc[: cur_i + 1])
    if len(h1) < htf_ema_period + 2:
        return None
    h1_close = h1["close"].values.astype(float)
    h1_ema = _ema(h1_close, htf_ema_period)
    h1_bias_long = h1_close[-1] > h1_ema[-1]
    h1_bias_short = h1_close[-1] < h1_ema[-1]

    cur_open = float(bars["open"].iloc[cur_i])
    cur_high = float(bars["high"].iloc[cur_i])
    cur_low = float(bars["low"].iloc[cur_i])
    cur_close = float(bars["close"].iloc[cur_i])

    for j in range(cur_i - 2, max(cur_i - 2 - ob_age_max, atr_period), -1):
        o_j = float(bars["open"].iloc[j])
        h_j = float(bars["high"].iloc[j])
        l_j = float(bars["low"].iloc[j])
        c_j = float(bars["close"].iloc[j])
        rng_j = h_j - l_j
        if rng_j <= 0:
            continue
        body_j = abs(c_j - o_j)
        br_j = body_j / rng_j
        if br_j < body_ratio_min:
            continue
        if j + 1 >= cur_i:
            continue
        o_k = float(bars["open"].iloc[j + 1])
        h_k = float(bars["high"].iloc[j + 1])
        l_k = float(bars["low"].iloc[j + 1])
        c_k = float(bars["close"].iloc[j + 1])
        rng_k = h_k - l_k

        # Bullish OB: prior red candle + green displacement close > prior high
        if c_j > o_j and c_k < o_k and rng_k >= displacement_k * atr and l_k < l_j:
            if not h1_bias_long:
                break
            ob_top = h_j
            ob_bot = l_j
            ob_mid = (ob_top + ob_bot) / 2.0
            if cur_low <= ob_top and cur_close > ob_mid and cur_open < cur_close:
                entry = ob_mid
                stop = ob_bot - buf
                risk = entry - stop
                if risk > 0:
                    return {"direction": "long", "entry": entry, "stop": stop,
                            "target": entry + rr * risk}
            break
        # Bearish OB
        if c_j < o_j and c_k > o_k and rng_k >= displacement_k * atr and h_k > h_j:
            if not h1_bias_short:
                break
            ob_top = h_j
            ob_bot = l_j
            ob_mid = (ob_top + ob_bot) / 2.0
            if cur_high >= ob_bot and cur_close < ob_mid and cur_open > cur_close:
                entry = ob_mid
                stop = ob_top + buf
                risk = stop - entry
                if risk > 0:
                    return {"direction": "short", "entry": entry, "stop": stop,
                            "target": entry - rr * risk}
            break
    return None