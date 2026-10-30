import numpy as np
import pandas as pd


def evaluate(bars, params):
    if bars.empty or len(bars) < 80:
        return None
    target_r = float(params.get("target_r", 2.0))
    tick = float(params.get("tick", 0.10))
    stop_buf_ticks = float(params.get("stop_buf_ticks", 6))
    disp_k = float(params.get("disp_k", 1.0))
    body_ratio_min = float(params.get("body_ratio_min", 0.45))
    fvg_lookback = int(params.get("fvg_lookback", 4))
    atr_period = int(params.get("atr_period", 14))
    start_min = int(params.get("session_start_min", 12 * 60))
    end_min = int(params.get("session_end_min", 17 * 60))

    h = bars["high"].values.astype(float)
    l = bars["low"].values.astype(float)
    c = bars["close"].values.astype(float)
    o = bars["open"].values.astype(float)
    n = len(bars)
    ts_v = bars["ts"].values

    try:
        ts_dt = pd.to_datetime(ts_v, utc=True, errors="coerce")
        hours = ts_dt.hour.values
        minutes = ts_dt.minute.values
    except Exception:
        return None

    last = n - 1
    if last < atr_period + fvg_lookback + 2:
        return None

    cur_min = int(hours[last]) * 60 + int(minutes[last])
    if cur_min < start_min or cur_min > end_min:
        return None

    rngs = h[last - atr_period: last] - l[last - atr_period: last]
    atr = float(np.mean(rngs))
    if atr <= 1e-9:
        return None
    buf = stop_buf_ticks * tick

    disp_idx = -1
    disp_dir = 0
    scan_start = max(last - 6, atr_period)
    for j in range(last, scan_start - 1, -1):
        rng_j = h[j] - l[j]
        body_j = abs(c[j] - o[j])
        if rng_j < disp_k * atr:
            continue
        if body_j < body_ratio_min * rng_j:
            continue
        if c[j] > o[j]:
            disp_dir = +1
        elif c[j] < o[j]:
            disp_dir = -1
        else:
            continue
        disp_idx = j
        break

    if disp_dir == 0 or disp_idx < 0:
        return None

    fvg_top = None
    fvg_bot = None
    for i in range(disp_idx, max(disp_idx - fvg_lookback, 0), -1):
        if i - 2 < 0:
            break
        h_prev = h[i - 2]
        l_cur = l[i]
        if disp_dir == +1 and h_prev < l_cur:
            fvg_top = l_cur
            fvg_bot = h_prev
            break
        l_prev = l[i - 2]
        h_cur = h[i]
        if disp_dir == -1 and l_prev > h_cur:
            fvg_top = l_prev
            fvg_bot = h_cur
            break

    if fvg_top is None or fvg_bot is None:
        return None

    fvg_mid = (fvg_top + fvg_bot) / 2.0

    c_cur = c[last]
    if disp_dir == +1:
        if c_cur < fvg_bot:
            return None
        entry = fvg_mid
        stop = l[disp_idx] - buf
        risk = entry - stop
        if risk <= 0.5 * tick:
            return None
        target = entry + risk * target_r
        return {"ts": ts_v[last], "direction": "long",
                "entry": entry, "stop": stop, "target": target}
    else:
        if c_cur > fvg_top:
            return None
        entry = fvg_mid
        stop = h[disp_idx] + buf
        risk = stop - entry
        if risk <= 0.5 * tick:
            return None
        target = entry - risk * target_r
        return {"ts": ts_v[last], "direction": "short",
                "entry": entry, "stop": stop, "target": target}
