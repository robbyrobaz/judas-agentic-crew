"""
iFVG Midpoint Reversion 15m with HTF Bias Filter.

Architecture: takes the proven 5m iFVG midpoint reversion (CSID 232) and adapts
to 15m bars. Same core logic:
- 3-candle FVG detection (bars[i-3].high < bars[i-1].low for bull; reverse for bear)
- middle candle must be directional (close > open for bull)
- min_gap_factor ratio of avg_range(20)
- Track setups; arm -> invert (close breaks opposite side of FVG) -> entry on retest of midpoint
- rr and zone_buffer for stop/target

HTF bias: rolling 60-bar mean of close (15h of 15m bars = ~1.5 US trading sessions)
- HTF bullish: close > mean -> only take bear FVG longs
- HTF bearish: close < mean -> only take bull FVG shorts

Differences from CSID 232 (5m):
- Timeframe: 15m vs 5m (slower, more selective signals)
- fvg_expiry: 20 15m bars = 5h vs 20 5m bars = 1.67h
- min_gap_factor: 0.25 (slightly looser to compensate for slower timeframe)
- rr: 2.0 (matches 5m baseline)

Trade-off vs CSID 232 5m: signal count drops but quality rises (fewer false positives).
"""
import numpy as np

def evaluate(bars, params):
    if bars is None or len(bars) < 50: return None
    n = len(bars)
    lookback = int(params.get('lookback', 20))
    min_gap_factor = float(params.get('min_gap_factor', 0.25))
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
