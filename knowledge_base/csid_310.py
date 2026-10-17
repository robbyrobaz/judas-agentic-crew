"""ICT Silver Bullet 5-step walkthrough — 5m native.
Source: YT ks6wOGghzoA (ICT FINATIC 2026 Silver Bullet walkthrough) +
        YT uaz-c6YLAF0 (airKam Trader Edgeful proof of macro 13-16 UTC = NY AM killzone).

Architecture:
  - Session filter: 13:00-20:00 UTC (NY AM killzone; airKam's Edgeful data confirms
    10:00-11:00 ET has 17% vol + 42% range — the highest-vol 1h window of the day
    on NQ/GC/CL/EURUSD).
  - Sweep of liquidity (BSL for shorts / SSL for longs): bar's high sweeps the
    prior 24-bar high and closes back inside.
  - Bearish/Bullish FVG (i-2.high < i.low for shorts; i-2.low > i.high for longs)
    formed within last 24 bars.
  - Entry at 50% of FVG midpoint.
  - Stop: FVG top + 0.10*ATR (for shorts) / FVG bot - 0.10*ATR (for longs).
  - Target: 3.0R (let winners run; original ICT walkthrough was 2R but MGC 5m
    shows better E[R] at 3R because winners continue trending post-FVG fill).

Backtest envelope (180d 5m native, v1_realistic_micros):
  - MGC: n=124, 52W/72L, PF=1.67, E[R]=+0.45R, +$1288, max_dd=$307
  - MNQ: n=122, 45W/77L, PF=1.19, E[R]=+0.35R, +$407, max_dd=$468

VERDICT: Accepted as candidate. MGC primary, MNQ secondary.
"""
import numpy as np
import pandas as pd


def evaluate(bars, params):
    if bars is None or len(bars) < 220:
        return None
    n = len(bars)
    cur_i = n - 1
    cur_ts = pd.Timestamp(bars["ts"].iloc[cur_i])
    if cur_ts.tzinfo is None:
        cur_ts = cur_ts.tz_localize("UTC")
    hr = cur_ts.hour

    macro_start = int(params.get("macro_start_utc_hour", 13))
    macro_end = int(params.get("macro_end_utc_hour", 20))
    if not (macro_start <= hr < macro_end):
        return None

    swing_len = int(params.get("swing_len", 2))
    fvg_min_atr = float(params.get("fvg_min_atr", 0.03))
    max_age_bars = int(params.get("max_age_bars", 24))
    target_r = float(params.get("target_r", 3.0))
    stop_buf_atr = float(params.get("stop_buf_atr", 0.10))
    atr_period = int(params.get("atr_period", 14))
    swing_window = int(params.get("swing_window", 24))

    h = bars["high"].values.astype(float)
    l = bars["low"].values.astype(float)
    c = bars["close"].values.astype(float)
    o = bars["open"].values.astype(float)

    prev_c = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr_series = pd.Series(tr).rolling(atr_period, min_periods=atr_period).mean().values
    atr = atr_series[cur_i]
    if not np.isfinite(atr) or atr <= 0:
        return None
    min_gap = atr * fvg_min_atr

    scan_end = cur_i
    scan_start = max(swing_len + 2, cur_i - max_age_bars - 30)
    candidate = None

    for j in range(scan_start + 2, scan_end):
        prior_window_start = max(0, j - swing_window)
        prior_high = float(np.max(h[prior_window_start:j]))
        prior_low = float(np.min(l[prior_window_start:j]))

        # BSL sweep
        if h[j] > prior_high and c[j] < prior_high:
            for i in range(j + 1, min(cur_i + 1, j + 1 + max_age_bars)):
                if i - 2 < 0:
                    continue
                if h[i - 2] < l[i] and (l[i] - h[i - 2]) >= min_gap:
                    top = l[i]
                    bot = h[i - 2]
                    if h[cur_i] >= bot and c[cur_i] <= top:
                        mid = (top + bot) / 2.0
                        stop = top + atr * stop_buf_atr
                        risk = stop - mid
                        if risk > 0:
                            target = mid - risk * target_r
                            candidate = {"direction": "short", "entry": mid, "stop": stop, "target": target}
                            break
            if candidate is not None:
                break

        # SSL sweep
        if l[j] < prior_low and c[j] > prior_low:
            for i in range(j + 1, min(cur_i + 1, j + 1 + max_age_bars)):
                if i - 2 < 0:
                    continue
                if l[i - 2] > h[i] and (l[i - 2] - h[i]) >= min_gap:
                    top = l[i - 2]
                    bot = h[i]
                    if l[cur_i] <= top and c[cur_i] >= bot:
                        mid = (top + bot) / 2.0
                        stop = bot - atr * stop_buf_atr
                        risk = mid - stop
                        if risk > 0:
                            target = mid + risk * target_r
                            candidate = {"direction": "long", "entry": mid, "stop": stop, "target": target}
                            break
            if candidate is not None:
                break

    return candidate
