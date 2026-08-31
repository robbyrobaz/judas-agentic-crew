"""
Tradence Strategy A: Liquidity Sweep + Unmitigated FVG (top-of-FVG entry)
with kill-zone session filter.

Architecture (from YT:dudHOoyOin0):
  1. SESSION GATE: London 07-10 UTC OR NY distribution 13-16 UTC
  2. SWEEP: prior swing H/L swept by current bar; current close back inside
  3. UNMITIGATED FVG: 3-candle FVG on opposite side, never violated after
     forming, in the post-sweep direction (the impulse that produced the
     continuation should leave a fresh FVG)
  4. ENTRY: At TOP of bullish FVG (or BOTTOM of bearish FVG) — buying the
     continuation after the sweep, not the reversion to midpoint
  5. TARGET: external liquidity = the swept level (1R-3R typically)
  6. STOP: just beyond the FVG (on the OPPOSITE side from the entry)

Distinguishing from CSID 232 (midpoint reversion):
  - CSID 232 enters AT the midpoint, expecting price to mean-revert
    back through the FVG and continue
  - This enters AT the TOP (extreme continuation), expecting the FVG
    to act as a launchpad for the impulse that JUST swept liquidity
"""
_S = {"last_processed_idx": -1}


def evaluate(bars, params):
    if bars is None or len(bars) < 60:
        return None
    n = len(bars)
    cur_i = n - 1
    if cur_i == _S["last_processed_idx"]:
        return None
    _S["last_processed_idx"] = cur_i

    import pandas as pd
    cur_ts = pd.Timestamp(bars["ts"].iloc[cur_i])
    if cur_ts.tzinfo is None:
        cur_ts = cur_ts.tz_localize("UTC")
    h = cur_ts.hour
    # Kill-zone: London 07-10 UTC OR NY 13-16 UTC (distribution phase)
    if not (7 <= h < 10 or 13 <= h < 16):
        return None

    sweep_lookback = int(params.get("sweep_lookback", 48))
    fvg_expiry = int(params.get("fvg_expiry", 12))
    min_gap_factor = float(params.get("min_gap_factor", 0.20))
    rr = float(params.get("rr", 2.5))
    zone_buffer = float(params.get("zone_buffer", 0.10))

    highs = bars["high"].values.astype(float)
    lows = bars["low"].values.astype(float)
    closes = bars["close"].values.astype(float)
    opens = bars["open"].values.astype(float)

    ch = highs[cur_i]
    cl = lows[cur_i]
    cc = closes[cur_i]
    co = opens[cur_i]

    if cur_i < sweep_lookback + fvg_expiry:
        return None

    # Compute average range for gap filter
    s = 0.0
    for j in range(cur_i - sweep_lookback, cur_i):
        s += highs[j] - lows[j]
    avg_range = s / sweep_lookback
    if avg_range <= 0:
        return None
    min_gap = avg_range * min_gap_factor

    # Detect sweep — exclude the last few bars from "prior high/low" calc
    pre_start = max(0, cur_i - sweep_lookback)
    pre_end = max(0, cur_i - 3)  # exclude current and 2 most recent
    if pre_end <= pre_start + 5:
        return None
    prior_high = float(highs[pre_start:pre_end].max())
    prior_low = float(lows[pre_start:pre_end].min())

    swept_high = ch > prior_high and cc < prior_high
    swept_low = cl < prior_low and cc > prior_low
    if not (swept_high or swept_low):
        return None

    # After sweeping sell-side (swept_low), look for a bullish FVG
    # (i.e. price impulse up that we missed — we buy the retest at its TOP)
    # After sweeping buy-side (swept_high), look for a bearish FVG (sell at BOTTOM)
    direction = None
    if swept_low:
        direction = "long"
    elif swept_high:
        direction = "short"
    if direction is None:
        return None

    # Scan recent bars for an unmitigated FVG aligned with our direction.
    # Bullish FVG (for long): c3.high < c1.low (gap up); c2 is the impulse candle.
    # Bearish FVG (for short): c3.low > c1.high (gap down); c2 is the impulse candle.
    for i in range(cur_i - 3, max(cur_i - fvg_expiry - 3, 2), -1):
        if i < 3:
            continue
        c3h = highs[i - 2]
        c3l = lows[i - 2]
        c1h = highs[i]
        c1l = lows[i]
        c2c = closes[i - 1]
        c2o = opens[i - 1]

        if direction == "long":
            # Bullish FVG: gap up
            if c3h >= c1l:
                continue
            if (c1l - c3h) < min_gap:
                continue
            if c2c <= c2o:
                continue
            top = c1l
            bot = c3h
            # UNMITIGATED: no close below bot since forming
            mitigated = False
            for k in range(i, cur_i):
                if closes[k] < bot:
                    mitigated = True
                    break
            if mitigated:
                continue
            # Current bar must TAP the FVG (low touches zone)
            if cl > top or cc < bot:
                continue
            # Enter at TOP of FVG (extreme continuation entry)
            entry = top
            zh = top - bot
            stop = bot - zh * zone_buffer
            risk = entry - stop
            if risk <= 0:
                continue
            target = entry + rr * risk
            return {"direction": "long", "entry": entry, "stop": stop, "target": target}

        else:  # short
            # Bearish FVG: gap down
            if c3l <= c1h:
                continue
            if (c3l - c1h) < min_gap:
                continue
            if c2c >= c2o:
                continue
            top = c3l
            bot = c1h
            mitigated = False
            for k in range(i, cur_i):
                if closes[k] > top:
                    mitigated = True
                    break
            if mitigated:
                continue
            if ch < bot or cc > top:
                continue
            # Enter at BOTTOM of FVG
            entry = bot
            zh = top - bot
            stop = top + zh * zone_buffer
            risk = stop - entry
            if risk <= 0:
                continue
            target = entry - rr * risk
            return {"direction": "short", "entry": entry, "stop": stop, "target": target}

    return None
