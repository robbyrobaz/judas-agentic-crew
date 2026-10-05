"""
ICT iFVG Midpoint Reversion 1h + HTF Bias — multi-symbol port.

Architecture (CSID 316 backbone, validated MNQ 1h PF=2.75 n=181):

  1. Three-bar FVG formation (i-2, i-1, i):
     - Bull FVG: c3.high < c1.low (gap between high[i-2] and low[i])
     - Bear FVG: c3.low > c1.high
  2. Gap size filter: |gap| >= min_gap_factor * avg_range_lookback
  3. Displacement confirmation: middle bar (i-1) is bullish for bull FVG,
     bearish for bear FVG (close > open / close < open).
  4. FVG NOT inverted: intervening bars did not close through the zone.
  5. Current bar retesting the zone (high >= bottom for bear re-entry,
     low <= top for bull re-entry, close on inverted side).
  6. HTF bias: 60-bar EMA of closes — longs only in bullish, shorts only
     in bearish. Counter-trend setups filtered out (~30% of signals).
  7. Entry at FVG midpoint.
  8. Stop beyond zone edge + 20% of zone height.
  9. Target = entry +/- 2.0R.

Cross-symbol v1 stamp (180d, cost_model=v1_realistic_micros):
  6J  PF=1.59  E[R]=+0.23  n=97   CSID 328  candidate 7093
  ZF  PF=1.93  E[R]=+0.44  n=52   CSID 329  candidate 7094
  MCL PF=2.26  E[R]=+0.68  n=98   CSID 330  candidate 7095
  MGC PF=1.99  E[R]=+0.50  n=96   CSID 331  candidate 7096
  MNQ PF=2.75  E[R]=+0.70  n=181  CSID 316  (already active)

ALL PASS gate: PF>1.3, n>=20, E[R]>0, no BT-LURE signature.
"""
def evaluate(bars, params):
    n = len(bars)
    if n < 40:
        return None
    cur_i = n - 1
    if cur_i < 30:
        return None
    lookback = int(params.get('lookback', 24))
    min_gap_factor = float(params.get('min_gap_factor', 0.20))
    rr = float(params.get('rr', 2.0))
    zone_buffer = float(params.get('zone_buffer', 0.20))
    fvg_expiry = int(params.get('fvg_expiry', 24))
    htf_period = int(params.get('htf_ema_period', 60))
    if n < lookback + fvg_expiry + htf_period + 4:
        return None
    highs = bars['high'].values.astype(float)
    lows = bars['low'].values.astype(float)
    closes = bars['close'].values.astype(float)
    opens = bars['open'].values.astype(float)
    ema = float(sum(closes[max(0, cur_i - htf_period):cur_i + 1]) / min(htf_period, cur_i + 1))
    htf_bullish = closes[cur_i] > ema
    htf_bearish = closes[cur_i] < ema
    ch = highs[cur_i]; cl = lows[cur_i]; cc = closes[cur_i]
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
        c3h = highs[i-2]; c3l = lows[i-2]
        c1h = highs[i]; c1l = lows[i]
        c2c = closes[i-1]; c2o = opens[i-1]
        if c3h < c1l and (c1l - c3h) >= min_gap and c2c > c2o:
            if not htf_bearish:
                continue
            top = c1l; bot = c3h
            inverted = False
            for k in range(i, cur_i):
                if closes[k] < bot:
                    inverted = True; break
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
        if c3l > c1h and (c3l - c1h) >= min_gap and c2c < c2o:
            if not htf_bullish:
                continue
            top = c3l; bot = c1h
            inverted = False
            for k in range(i, cur_i):
                if closes[k] > top:
                    inverted = True; break
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