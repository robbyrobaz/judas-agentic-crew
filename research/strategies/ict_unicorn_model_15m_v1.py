"""ICT Unicorn Model — Breaker Block + FVG overlap on 15m bars.

Architecture (per ICT 2026 mentorship, finding 6047):
  A Breaker Block = a former Order Block that has been VIOLATED (price closed
  through it) and FLIPS role (demand→supply or supply→demand).
  A Unicorn = the overlap zone of a fresh Breaker Block + an unmitigated FVG.

  Implementation:
    1. Detect recent swing high/low breaks on the last 3-20 bars
       - If low[break] violated prior demand OB (low of an up-move), that OB
         becomes a Breaker (now supply at the top of the original move)
    2. Find an unmitigated FVG in the last 10 bars
    3. Compute the overlap zone of (a) Breaker price range and (b) FVG zone
       If overlap > 0 → Unicorn zone.
    4. Entry: wait for bar to retrace INTO the Unicorn zone (close within zone)
       Then enter in original direction (against the sweep) with stop beyond zone
       Target: 1:2 or 1:3 RR to next structure (use 2R default)

  For 15m bars this is more conservative than 5m (less noise) — should produce
  fewer but cleaner setups.
"""


def evaluate(bars, params):
    if bars is None or len(bars) < 60:
        return None

    # ---- params
    ob_lookback = int(params.get("ob_lookback", 6))         # OB detection window
    fvg_lookback = int(params.get("fvg_lookback", 10))      # FVG detection window
    break_lookback = int(params.get("break_lookback", 12))  # how recent the violation
    min_atr_frac = float(params.get("min_atr_frac", 0.5))   # min OB/FVG size in ATR
    max_atr_frac = float(params.get("max_atr_frac", 4.0))   # max OB/FVG size in ATR
    target_r = float(params.get("target_r", 2.0))
    stop_buf_atr = float(params.get("stop_buf_atr", 0.10))
    atr_period = int(params.get("atr_period", 14))
    session_filter = params.get("session_filter", "none")

    h = bars["high"].astype(float).values
    l = bars["low"].astype(float).values
    c = bars["close"].astype(float).values
    o = bars["open"].astype(float).values
    n = len(c)
    if n < max(ob_lookback, fvg_lookback, break_lookback) + atr_period + 5:
        return None

    if session_filter != "none":
        try:
            hr = int(bars.index[-1].hour)
        except Exception:
            hr = -1
        if session_filter == "ny" and not (12 <= hr <= 20):
            return None
        if session_filter == "london" and not (7 <= hr <= 11):
            return None

    # ATR
    tr = [0.0] * n
    tr[0] = h[0] - l[0]
    for i in range(1, n):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    atr_v = [0.0] * n
    atr_v[atr_period - 1] = sum(tr[:atr_period]) / atr_period
    for i in range(atr_period, n):
        atr_v[i] = (atr_v[i - 1] * (atr_period - 1) + tr[i]) / atr_period
    atr_now = atr_v[-1]
    if atr_now <= 0:
        return None

    min_ob_size = min_atr_frac * atr_now
    max_ob_size = max_atr_frac * atr_now

    # ---- Detect Bullish Breaker (demand OB that got violated by a wick/close below)
    # Pattern: a recent up-move created a demand OB (last bullish bar before strong up-move)
    # then price later dipped below that OB's low → that OB becomes supply now.
    #
    # Simpler proxy: a "violated bullish candle" — a candle whose low violated
    # the low of the prior up-move's bullish candle. That violated candle's
    # body becomes a supply zone (bearish OB / breaker).
    #
    # We scan the last break_lookback bars for such violations.
    breaker_supply_high = None
    breaker_supply_low = None
    breaker_demand_high = None
    breaker_demand_low = None
    for i in range(n - break_lookback, n - 1):
        if i < ob_lookback + 1:
            continue
        # Check if bar i closed below prior bullish OB low
        # Prior OB = highest close in the ob_lookback before bar i where close > open
        for j in range(max(0, i - ob_lookback), i):
            if c[j] > o[j]:  # bullish OB candidate
                ob_low = min(l[j], o[j])
                ob_high = max(h[j], o[j])
                if l[i] < ob_low and c[i] < ob_low:  # violated the OB low and CLOSED below
                    # Bar j is now a Breaker (supply zone)
                    breaker_supply_high = ob_high
                    breaker_supply_low = ob_low
                    break
        # Check if bar i closed above prior bearish OB high → demand breaker
        for j in range(max(0, i - ob_lookback), i):
            if c[j] < o[j]:  # bearish OB candidate
                ob_high = max(h[j], o[j])
                ob_low = min(l[j], o[j])
                if h[i] > ob_high and c[i] > ob_high:
                    breaker_demand_high = ob_high
                    breaker_demand_low = ob_low
                    break
        if breaker_supply_high is not None or breaker_demand_high is not None:
            break

    # ---- Detect recent unmitigated FVG (3-bar pattern, gap between bar1.high and bar3.low)
    # Bullish FVG: bar1.high < bar3.low → gap from bar1.high to bar3.low (demand)
    # Bearish FVG: bar1.low > bar3.high → gap from bar3.high to bar1.low (supply)
    fvg_bull_high = None  # top of bullish FVG = bar3.low (newer)
    fvg_bull_low = None   # bottom of bullish FVG = bar1.high (older)
    fvg_bear_high = None  # top of bearish FVG = bar1.low (older)
    fvg_bear_low = None   # bottom of bearish FVG = bar3.high (newer)
    for i in range(n - fvg_lookback, n - 2):
        if i < 1:
            continue
        # Bullish FVG: gap from h[i-1] up to l[i+1]
        bull_gap = l[i + 1] - h[i - 1]
        if bull_gap > min_ob_size and bull_gap < max_ob_size:
            fvg_bull_high = l[i + 1]
            fvg_bull_low = h[i - 1]
        # Bearish FVG: gap from l[i-1] down to h[i+1]
        bear_gap = l[i - 1] - h[i + 1]
        if bear_gap > min_ob_size and bear_gap < max_ob_size:
            fvg_bear_high = l[i - 1]
            fvg_bear_low = h[i + 1]
        if fvg_bull_high is not None or fvg_bear_high is not None:
            break

    # ---- Look for Unicorn overlap
    # Bearish Unicorn: short signal at supply (Breaker supply + Bearish FVG overlap)
    # Bullish Unicorn: long signal at demand (Breaker demand + Bullish FVG overlap)
    # For SHORT: look for bullish bar in recent past that has now reversed (close < open
    # at the overlap zone) — i.e., current bar is bearish and closes inside zone
    signal = None
    if breaker_supply_high is not None and fvg_bear_high is not None:
        ov_high = min(breaker_supply_high, fvg_bear_high)
        ov_low = max(breaker_supply_low, fvg_bear_low)
        if ov_high > ov_low:  # real overlap
            zone_high = ov_high
            zone_low = ov_low
            zone_size = zone_high - zone_low
            # Entry: last bar closes inside zone (overlap is supply, expect rejection)
            # Use bar[-1] as trigger
            last_close = c[-1]
            last_open = o[-1]
            last_high = h[-1]
            last_low = l[-1]
            if last_close < last_open:  # bearish bar
                if last_low <= zone_high and last_high >= zone_low:  # touches zone
                    entry = last_close
                    stop = zone_high + stop_buf_atr * atr_now
                    risk = stop - entry
                    if risk > 0:
                        target = entry - target_r * risk
                        signal = {
                            "direction": "short",
                            "entry": float(entry),
                            "stop": float(stop),
                            "target": float(target),
                        }

    if signal is None and breaker_demand_high is not None and fvg_bull_high is not None:
        ov_high = min(breaker_demand_high, fvg_bull_high)
        ov_low = max(breaker_demand_low, fvg_bull_low)
        if ov_high > ov_low:
            zone_high = ov_high
            zone_low = ov_low
            zone_size = zone_high - zone_low
            last_close = c[-1]
            last_open = o[-1]
            last_high = h[-1]
            last_low = l[-1]
            if last_close > last_open:
                if last_low <= zone_high and last_high >= zone_low:
                    entry = last_close
                    stop = zone_low - stop_buf_atr * atr_now
                    risk = entry - stop
                    if risk > 0:
                        target = entry + target_r * risk
                        signal = {
                            "direction": "long",
                            "entry": float(entry),
                            "stop": float(stop),
                            "target": float(target),
                        }

    return signal
