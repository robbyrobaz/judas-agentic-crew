import numpy as np
import pandas as pd


def evaluate(bars, params):
    """MNQ directional filter: RSI cross + 20d SMA regime gate (per-bar)."""
    if bars.empty or len(bars) < 50:
        return None
    period = int(params.get("period", 10))
    lo_thr = float(params.get("lo_thr", 30))
    hi_thr = float(params.get("hi_thr", 70))
    target_r = float(params.get("target_r", 1.5))
    stop_atr = float(params.get("stop_atr_mult", 1.0))
    sma_len = int(params.get("sma_len", 480))
    h = bars["high"].values.astype(float)
    l = bars["low"].values.astype(float)
    c = bars["close"].values.astype(float)
    o = bars["open"].values.astype(float)
    n = len(bars)
    ts_v = bars["ts"].values
    prev_c = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum.reduce([
        h - l,
        np.abs(h - prev_c),
        np.abs(l - prev_c),
    ])
    atr = pd.Series(tr).rolling(14, min_periods=5).mean().values
    sma = pd.Series(c).rolling(sma_len, min_periods=20).mean().values
    delta = np.concatenate([[0], np.diff(c)])
    gains = np.where(delta > 0, delta, 0)
    losses = np.where(delta < 0, -delta, 0)
    avg_gain = pd.Series(gains).rolling(period).mean().values
    avg_loss = pd.Series(losses).rolling(period).mean().values
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = np.where(avg_loss > 0, avg_gain / avg_loss, np.nan)
        rsi = 100 - (100 / (1 + rs))
    last = n - 1
    if last < 1 or np.isnan(atr[last]) or np.isnan(sma[last]) or np.isnan(rsi[last]) or np.isnan(rsi[last - 1]):
        return None
    if rsi[last - 1] < lo_thr and rsi[last] >= lo_thr and c[last] < sma[last]:
        entry = o[last]
        stop = entry - atr[last] * stop_atr
        target = entry + (entry - stop) * target_r
        return {"ts": ts_v[last], "direction": "long",
                "entry": entry, "stop": stop, "target": target}
    elif rsi[last - 1] > hi_thr and rsi[last] <= hi_thr and c[last] > sma[last]:
        entry = o[last]
        stop = entry + atr[last] * stop_atr
        target = entry - (stop - entry) * target_r
        return {"ts": ts_v[last], "direction": "short",
                "entry": entry, "stop": stop, "target": target}
    return None