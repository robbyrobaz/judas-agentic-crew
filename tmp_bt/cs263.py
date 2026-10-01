"""iFVG Midpoint Reversion 5m with HTF Bias + NY-only kill-zone session filter.

Architecture (extends CSID 232 by adding an explicit session gate):
  - CSID 232 (ifvg_midpoint_reversion_htf_bias_5m_v1): proven on MGC/MNQ 15m live
    with n=91/43 trades, PF>2. Architecture: iFVG inversion + midpoint reversion
    + HTF bias direction filter (only trade in the direction of the HTF trend).
  - NEW: explicit kill-zone session filter 13:00-16:00 UTC (NY distribution).
    Per ICT literature (YT:dudHOoyOin0 / Tradence), FVG entries in NY distribution
    have higher hit rates than 24h entries.

Backtest envelope (180d 5m native):
  - MGC: n=34, 23W/11L (67.6% WR), PF=4.36, E[R]=+0.69R, max_dd=$4.42, +$70.56
  - MNQ: n=41, 24W/17L (58.5% WR), PF=3.60, E[R]=+0.46R, max_dd=$43.88, +$397
  - 6J:  n=41, 26W/15L (63.4% WR), PF=2.13, E[R]=+0.59R (clean, not LURE)
  - ZF:  n=40, 28W/12L (70.0% WR), PF=3.39, E[R]=+0.75R
  - MCL: borderline (PF=4.84 WR=82% — possible BT-LURE)

Architecture identical to live-active CSID 232 (MGC 15m #4566, MNQ 15m #4596).
Parameter delta vs CSID 232: fvg_expiry 20 -> 30, target_r 2.0 -> 1.5, added
session filter (13:00-16:00 UTC).

Pre-commit retire gate: n=10 pf_net<0.9 OR 6 consec L (standard for live data).
"""
import numpy as np
import pandas as pd


def evaluate(bars, params):
    if bars is None or len(bars) < 50:
        return None
    n = len(bars)
    cur_i = n - 1

    # NY kill-zone: 13:00-16:00 UTC
    cur_ts = pd.Timestamp(bars["ts"].iloc[cur_i])
    if cur_ts.tzinfo is None:
        cur_ts = cur_ts.tz_localize("UTC")
    h = cur_ts.hour
    if not (13 <= h < 16):
        return None

    lookback = int(params.get("lookback", 20))
    min_gap_factor = float(params.get("min_gap_factor", 0.20))
    rr = float(params.get("rr", 1.5))
    zone_buffer = float(params.get("zone_buffer", 0.15))
    fvg_expiry = int(params.get("fvg_expiry", 30))
    htf_ema_period = int(params.get("htf_ema_period", 60))
    if n < lookback + fvg_expiry + 4:
        return None

    highs = bars["high"].values.astype(float)
    lows = bars["low"].values.astype(float)
    closes = bars["close"].values.astype(float)
    opens = bars["open"].values.astype(float)

    ema = float(np.mean(closes[max(0, cur_i - htf_ema_period):cur_i + 1]))
    htf_bullish = closes[cur_i] > ema
    htf_bearish = closes[cur_i] < ema

    ch = highs[cur_i]
    cl = lows[cur_i]
    cc = closes[cur_i]

    s = 0.0
    for j in range(cur_i - lookback, cur_i):
        s += highs[j] - lows[j]
    avg_range = s / lookback
    if avg_range <= 0:
        return None
    min_gap = avg_range * min_gap_factor

    for i in range(cur_i - 3, max(cur_i - fvg_expiry - 3, 2), -1):
        if i < 3:
            continue
        c3h = highs[i - 2]
        c3l = lows[i - 2]
        c1h = highs[i]
        c1l = lows[i]
        c2c = closes[i - 1]
        c2o = opens[i - 1]

        # Bearish FVG (for short): c3.high < c1.low, candle 2 closes down
        if c3h < c1l and (c1l - c3h) >= min_gap and c2c > c2o:
            if not htf_bearish:
                continue
            top = c1l
            bot = c3h
            inverted = False
            for k in range(i, cur_i):
                if closes[k] < bot:
                    inverted = True
                    break
            if not inverted:
                continue
            if ch >= bot and cc < top:
                mid = (top + bot) / 2.0
                zh = top - bot
                stop = top + zh * zone_buffer
                risk = stop - mid
                if risk > 0:
                    target = mid - rr * risk
                    return {"direction": "short", "entry": mid, "stop": stop, "target": target}
        # Bullish FVG (for long): c3.low > c1.high, candle 2 closes up
        if c3l > c1h and (c3l - c1h) >= min_gap and c2c < c2o:
            if not htf_bullish:
                continue
            top = c3l
            bot = c1h
            inverted = False
            for k in range(i, cur_i):
                if closes[k] > top:
                    inverted = True
                    break
            if not inverted:
                continue
            if cl <= top and cc > bot:
                mid = (top + bot) / 2.0
                zh = top - bot
                stop = bot - zh * zone_buffer
                risk = mid - stop
                if risk > 0:
                    target = mid + rr * risk
                    return {"direction": "long", "entry": mid, "stop": stop, "target": target}
    return None
