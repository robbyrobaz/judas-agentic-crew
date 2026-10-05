"""
ICT Asian Range Judas Swing — 15m variant.

Architecture (URAH ICT OTE+FVG walkthrough + ICT London open Judas convention):

  1. ASIAN RANGE = high/low of bars in 00:00-07:00 UTC same day.
  2. JUDAS WINDOW = bars in 07:00-10:00 UTC (London open, when Judas
     swings typically occur).
  3. JUDAS SWEEP: current bar wicks beyond the Asian range extreme
     (low < asia_low OR high > asia_high) AND closes back inside.
  4. DISPLACEMENT: bar_range >= 0.4 * ATR(14) AND body_ratio >= 0.45.
  5. ENTRY at bar close after the Judas sweep.
  6. STOP beyond sweep extreme + 0.2 * ATR.
  7. TARGET = 1.5R (continuation target).
  8. ASIAN RANGE GATE: range >= 0.5 ATR (reject compressed Asian sessions).

Why fresh family:
  - Existing judas_continuation uses HTF pivot sweeps, NOT daily Asian range.
  - Existing bsl_run_judas uses BSL/SSL EQ clusters, NOT time-sweep.
  - Existing ote_displacement_reversion uses swing pivots + Fib retracements,
    not bar-close re-entry after sweep.
  - Asian range Judas is a documented ICT 2026 setup (separate from
    silver bullet PDH/PDL which uses prior-day levels).

DIFFERENTIATION:
  - Different anchor (Asian session vs prior-day swing vs rolling PDH/PDL).
  - Different session filter (London open vs NY 13-16 vs no filter).
  - Different entry trigger (close back inside range after sweep).

Backtest config: cost_model=v1_realistic_micros, 90d + 180d.
"""
import numpy as np
import pandas as pd


def evaluate(bars, params):
    if bars is None or len(bars) < 120:
        return None

    asian_start = int(params.get("asian_start", 0))
    asian_end = int(params.get("asian_end", 7))
    judas_start = int(params.get("judas_start", 7))
    judas_end = int(params.get("judas_end", 10))
    disp_atr_mult = float(params.get("disp_atr_mult", 0.4))
    body_ratio_min = float(params.get("body_ratio_min", 0.45))
    target_r = float(params.get("target_r", 1.5))
    stop_buf_atr = float(params.get("stop_buf_atr", 0.2))
    asian_min_atr = float(params.get("asian_min_atr", 0.5))
    atr_period = int(params.get("atr_period", 14))

    n = len(bars)
    cur_i = n - 1

    h = bars["high"].values.astype(float)
    l = bars["low"].values.astype(float)
    c = bars["close"].values.astype(float)
    o = bars["open"].values.astype(float)

    try:
        cur_ts = pd.to_datetime(bars["ts"].iloc[cur_i], utc=True, errors="coerce")
        if pd.isna(cur_ts):
            return None
        hr = int(cur_ts.hour)
        cur_date = cur_ts.date
    except Exception:
        return None

    if hr < judas_start or hr >= judas_end:
        return None

    h_series = bars["high"].astype(float)
    l_series = bars["low"].astype(float)
    c_series = bars["close"].astype(float)
    pc = c_series.shift(1)
    tr = pd.concat([h_series - l_series, (h_series - pc).abs(), (l_series - pc).abs()], axis=1).max(axis=1)
    atr_series = tr.ewm(alpha=1.0 / atr_period, adjust=False).mean().values
    if cur_i < atr_period or np.isnan(atr_series[cur_i]) or atr_series[cur_i] <= 0:
        return None
    cur_atr = float(atr_series[cur_i])

    ts = pd.to_datetime(bars["ts"], utc=True, errors="coerce")
    if ts.isna().any():
        return None

    asian_mask = (
        (ts.dt.hour >= asian_start)
        & (ts.dt.hour < asian_end)
        & (ts.dt.date == cur_date)
    ).values
    asian_idx = np.where(asian_mask)[0]
    if len(asian_idx) < 5:
        return None

    asian_high = float(np.max(h[asian_idx]))
    asian_low = float(np.min(l[asian_idx]))
    asian_range = asian_high - asian_low
    if asian_range < asian_min_atr * cur_atr:
        return None

    bar_range = h[cur_i] - l[cur_i]
    if bar_range <= 0:
        return None
    body = abs(c[cur_i] - o[cur_i])
    if body / bar_range < body_ratio_min:
        return None
    if bar_range < disp_atr_mult * cur_atr:
        return None

    direction = 0
    if l[cur_i] < asian_low and c[cur_i] > asian_low:
        direction = 1
    elif h[cur_i] > asian_high and c[cur_i] < asian_high:
        direction = -1

    if direction == 0:
        return None

    if direction == 1:
        entry_price = float(c[cur_i])
        stop = float(l[cur_i]) - stop_buf_atr * cur_atr
        risk = entry_price - stop
        target = entry_price + target_r * risk
    else:
        entry_price = float(c[cur_i])
        stop = float(h[cur_i]) + stop_buf_atr * cur_atr
        risk = stop - entry_price
        target = entry_price - target_r * risk

    if risk <= 0:
        return None

    return {
        "direction": "long" if direction == 1 else "short",
        "entry": entry_price,
        "stop": stop,
        "target": target,
    }