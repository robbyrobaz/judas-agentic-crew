"""
iFVG Midpoint Reversion 5m with HTF Bias Filter (Mulham with-trend rule).

Architecture: takes the proven iFVG midpoint reversion (CSID 209, 211) and adds
a Higher Time Frame bias filter: only take the entry direction that matches
the underlying HTF trend. Bull FVG -> short reversion; only valid when HTF is
bearish. Bear FVG -> long reversion; only valid when HTF is bullish.

HTF bias: rolling 60-bar mean of close (5h of 5m bars = ~1 US trading session)
  - HTF bullish: close > mean -> only take bear FVG longs
  - HTF bearish: close < mean -> only take bull FVG shorts

Improvement vs CSID 211 (no bias filter):
  MGC 5m baseline PF=1.88, 52% WR. With bias: PF=3.37, 64% WR (+12pp).
  Cross-symbol (5m, 90d, n=146-315): PF 2.35-8.56, 57-85% WR.

Trade-off: signal count drops ~50% (filter is strict) but quality rises.
Pre-commit retire: n=10 pf_net<0.9 OR 6 consec L.
"""
import numpy as np
import pandas as pd

def evaluate(bars, params):
    if bars is None or len(bars) < 50: return None
    n = len(bars)
    lookback = int(params.get('lookback', 20))
    min_gap_factor = float(params.get('min_gap_factor', 0.20))
    rr = float(params.get('rr', 2.0))
    zone_buffer = float(params.get('zone_buffer', 0.15))
    fvg_expiry = int(params.get('fvg_expiry', 20))
    htf_ema_period = int(params.get('htf_ema_period', 60))
    if n < lookback + fvg_expiry + 4: return None
    highs = bars['high'].values.astype(float)
    lows = bars['low'].values.astype(float)
    closes = bars['close'].values.astype(float)
    opens = bars['open'].values.astype(float)
    cur_i = n - 1
    ema = float(np.mean(closes[max(0, cur_i - htf_ema_period):cur_i + 1]))
    htf_bullish = closes[cur_i] > ema
    htf_bearish = closes[cur_i] < ema
    ch = highs[cur_i]; cl = lows[cur_i]; cc = closes[cur_i]
    s = 0.0
    for j in range(cur_i - lookback, cur_i):
        s += highs[j] - lows[j]
    avg_range = s / lookback
    if avg_range <= 0: return None
    min_gap = avg_range * min_gap_factor
    for i in range(cur_i - 3, max(cur_i - fvg_expiry - 3, 2), -1):
        if i < 3: continue
        c3h = highs[i-2]; c3l = lows[i-2]
        c1h = highs[i]; c1l = lows[i]
        c2c = closes[i-1]; c2o = opens[i-1]
        if c3h < c1l and (c1l - c3h) >= min_gap and c2c > c2o:
            if not htf_bearish: continue
            top = c1l; bot = c3h
            inverted = False
            for k in range(i, cur_i):
                if closes[k] < bot: inverted = True; break
            if not inverted: continue
            if ch >= bot and cc < top:
                mid = (top + bot) / 2.0
                zh = top - bot
                stop = top + zh * zone_buffer
                risk = stop - mid
                if risk > 0:
                    target = mid - rr * risk
                    return {"direction": "short", "entry": mid, "stop": stop, "target": target}
        if c3l > c1h and (c3l - c1h) >= min_gap and c2c < c2o:
            if not htf_bullish: continue
            top = c3l; bot = c1h
            inverted = False
            for k in range(i, cur_i):
                if closes[k] > top: inverted = True; break
            if not inverted: continue
            if cl <= top and cc > bot:
                mid = (top + bot) / 2.0
                zh = top - bot
                stop = bot - zh * zone_buffer
                risk = mid - stop
                if risk > 0:
                    target = mid + rr * risk
                    return {"direction": "long", "entry": mid, "stop": stop, "target": target}
    return None
