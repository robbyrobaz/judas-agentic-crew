_S = {"last_processed_idx": -1}

def evaluate(bars, params):
    """Liquidity Sweep + iFVG Combined Entry (session-gated).

    Architecture:
      1. SESSION GATE: London 07-10 UTC OR NY 13-16 UTC
      2. SWEEP DETECT: Prior session high (max high of last 48 5m bars) is swept
         by current bar high, AND current close is back inside the range
      3. POST-SWEEP iFVG: After sweep, a 3-candle FVG forms on opposite side,
         with subsequent close crossing through it (inversion)
      4. RETEST: Current bar touches the iFVG zone while on inverted side
      5. ENTRY: At iFVG midpoint, stop outside zone, target = 2R

    Distinct from CSID 209/211: requires HTF sweep + post-sweep iFVG.
    Inspired by: YT:2VbkC2kJALo (Smart Risk).
    """
    import pandas as pd
    n = len(bars)
    if n < 50: return None
    cur_i = n - 1
    if cur_i == _S["last_processed_idx"]: return None
    _S["last_processed_idx"] = cur_i
    cur_ts = pd.Timestamp(bars["ts"].iloc[cur_i])
    if cur_ts.tzinfo is None: cur_ts = cur_ts.tz_localize("UTC")
    h = cur_ts.hour
    if not (7 <= h < 10 or 13 <= h < 16): return None
    sweep_lookback = int(params.get("sweep_lookback", 48))
    fvg_lookback = int(params.get("fvg_lookback", 24))
    min_gap_factor = float(params.get("min_gap_factor", 0.15))
    rr = float(params.get("rr", 2.0))
    zone_buffer = float(params.get("zone_buffer", 0.20))
    highs = bars["high"].values.astype(float)
    lows = bars["low"].values.astype(float)
    closes = bars["close"].values.astype(float)
    opens = bars["open"].values.astype(float)
    ch = highs[cur_i]; cl = lows[cur_i]; cc = closes[cur_i]
    pre_sweep_start = max(0, cur_i - sweep_lookback)
    pre_sweep_end = max(0, cur_i - fvg_lookback)
    if pre_sweep_end <= pre_sweep_start: return None
    avg_range = sum(highs[j] - lows[j] for j in range(cur_i - sweep_lookback, cur_i)) / sweep_lookback
    if avg_range <= 0: return None
    prior_high = float(highs[pre_sweep_start:pre_sweep_end].max())
    prior_low = float(lows[pre_sweep_start:pre_sweep_end].min())
    swept_high = ch > prior_high and cc < prior_high
    swept_low = cl < prior_low and cc > prior_low
    if not (swept_high or swept_low): return None
    min_gap = avg_range * min_gap_factor
    best_short_fvg = None
    best_long_fvg = None
    if swept_high:
        for i in range(cur_i - 3, max(2, cur_i - fvg_lookback), -1):
            c3h = highs[i-2]; c3l = lows[i-2]
            c1h = highs[i]; c1l = lows[i]
            c2c = closes[i-1]; c2o = opens[i-1]
            if c3h < c1l and (c1l - c3h) >= min_gap and c2c > c2o:
                top = c1l; bot = c3h
                inverted = False
                for k in range(i, cur_i):
                    if closes[k] < bot:
                        inverted = True; break
                if inverted: best_short_fvg = (top, bot); break
    if swept_low:
        for i in range(cur_i - 3, max(2, cur_i - fvg_lookback), -1):
            c3l_ = lows[i-2]; c3h_ = highs[i-2]
            c1l_ = lows[i]; c1h_ = highs[i]
            c2c = closes[i-1]; c2o = opens[i-1]
            if c3l_ > c1h_ and (c3l_ - c1h_) >= min_gap and c2c < c2o:
                top = c3l_; bot = c1h_
                inverted = False
                for k in range(i, cur_i):
                    if closes[k] > top:
                        inverted = True; break
                if inverted: best_long_fvg = (top, bot); break
    if best_short_fvg is not None:
        top, bot = best_short_fvg
        if ch >= bot and cc < top:
            mid = (top + bot) / 2.0
            zh = top - bot
            stop = top + zh * zone_buffer
            risk = stop - mid
            if risk > 0:
                return {"direction": "short", "entry": mid, "stop": stop, "target": mid - rr * risk}
    if best_long_fvg is not None:
        top, bot = best_long_fvg
        if cl <= top and cc > bot:
            mid = (top + bot) / 2.0
            zh = top - bot
            stop = bot - zh * zone_buffer
            risk = mid - stop
            if risk > 0:
                return {"direction": "long", "entry": mid, "stop": stop, "target": mid + rr * risk}
    return None
