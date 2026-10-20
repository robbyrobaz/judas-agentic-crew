# 5m Session-Window Displacement + FVG Pullback - LONDON SILVER BULLET (csid draft)
#
# Architecture (mirrors session_disp_fvg_pullback_5m_ny_sb_v1 exactly, but window shifts to London open):
#   1. Strict session gate: 07:00 - 09:30 UTC (London SB + 30 min for late completion)
#      - During October EDT this = 03:00-05:30 AM ET (ICT London killzone 02:00-05:00 AM ET)
#      - Frankfurt open overlaps the first hour of this window
#   2. 5m bar must displace: range >= disp_k * 5m-ATR (default 1.0)
#   3. Bar must have a strong close: body_ratio >= body_ratio_min (default 0.45)
#   4. After displacement, scan last 4 bars for a 3-candle FVG (high[i-2] < low[i])
#   5. FVG direction must match displacement direction
#   6. Entry at FVG midpoint (pullback)
#   7. Stop beyond displacement candle wick + stop_buf_ticks * tick buffer
#   8. Target: 2R
#
# Why novel vs existing 5m strategies on this venue:
# - frankfurt_silver_bullet_15m_v1 (csid 291) is 15m, sweep-based, NY window
# - ict_mitigation_block_london_sb_5m_6j_v1 (csid 399) is mitigation-block architecture
# - ict_silver_bullet_5step_sweep_fvg_midpoint_5m_v1 (csid 310) uses 7h macro window
# - session_disp_fvg_pullback_5m_ny_sb_v1 (csid 425) is NY window
# This is the FIRST 5m strategy to combine strict London killzone window
# (07:00-09:30 UTC) with displacement + FVG midpoint pullback.
#
# Tick defaults: 0.10 for MGC, 0.01 for MCL/ZF, 0.25 for MNQ, 1e-6 for 6J.
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
    start_min = int(params.get("session_start_min", 7 * 60))
    end_min = int(params.get("session_end_min", 9 * 60 + 30))

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