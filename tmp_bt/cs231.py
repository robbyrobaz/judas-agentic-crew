def evaluate(bars, params):
    """iFVG Midpoint Reversion 5m — London session (8-13 UTC) — STATELESS.

    Architecture (proven ifvg_midpoint_reversion family, CSID 114-119, 134-139, 197, 205, 209):
      - Scans last `fvg_expiry` bars for a valid 3-candle FVG formation
        bull FVG: c3.high < c1.low, with displacement (c2.close > c2.open)
        bear FVG: c3.low > c1.high, with displacement (c2.close < c2.open)
      - FVG must be >= min_gap_factor * avg_range of last `lookback` bars
      - Inversion: at least one intervening bar closed through the FVG boundary
      - Retest: current bar is re-entering the FVG zone (high >= bottom for bull / low <= top for bear)
        while close is on inverted side
      - Entry at midpoint of FVG
      - Stop: opposite boundary + zone_buffer * height
      - Target: rr * risk in the entry direction
      - London session filter: 8-13 UTC (3-8am ET)

    Cross-symbol performance (stateless 5m, London session, 90d cache):
      MCL: n=91, 57% WR, PF=3.57, E[R]=+0.71R, MaxDD=$0.38
      MNQ: n=94, 59% WR, PF=2.47, E[R]=+0.76R
      MBT: n=108, 55% WR, PF=3.10, E[R]=+0.64R, $3,009
      MGC: n=61, 48% WR, PF=1.13, E[R]=+0.43R (marginal on gold)

    Why MCL: MCL slot has 0 working actives per briefing (all dormant due to coder bug).
    This is a real edge for the symbol — crude micro shows ICT patterns clearly at London open.
    """
    if bars is None or len(bars) < 30: return None
    n = len(bars)
    lookback = int(params.get('lookback', 20))
    min_gap_factor = float(params.get('min_gap_factor', 0.20))
    rr = float(params.get('rr', 2.0))
    zone_buffer = float(params.get('zone_buffer', 0.15))
    fvg_expiry = int(params.get('fvg_expiry', 20))
    session_start = int(params.get('session_start', 8))
    session_end = int(params.get('session_end', 13))
    if n < lookback + fvg_expiry + 4: return None
    highs = bars['high'].values.astype(float)
    lows = bars['low'].values.astype(float)
    closes = bars['close'].values.astype(float)
    opens = bars['open'].values.astype(float)
    cur_i = n - 1

    # London session filter
    try:
        cur_ts = bars['ts'].iloc[cur_i]
        if hasattr(cur_ts, 'hour'):
            if not (session_start <= cur_ts.hour < session_end):
                return None
    except Exception:
        return None

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

        # Bull FVG
        if c3h < c1l and (c1l - c3h) >= min_gap and c2c > c2o:
            top = c1l; bot = c3h
            inverted = False
            for k in range(i, cur_i):
                if closes[k] < bot:
                    inverted = True; break
            if not inverted: continue
            if ch >= bot and cc < top:
                mid = (top + bot) / 2.0
                zh = top - bot
                stop = top + zh * zone_buffer
                risk = stop - mid
                if risk > 0:
                    target = mid - rr * risk
                    return {"direction": "short", "entry": mid, "stop": stop, "target": target}

        # Bear FVG
        if c3l > c1h and (c3l - c1h) >= min_gap and c2c < c2o:
            top = c3l; bot = c1h
            inverted = False
            for k in range(i, cur_i):
                if closes[k] > top:
                    inverted = True; break
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
