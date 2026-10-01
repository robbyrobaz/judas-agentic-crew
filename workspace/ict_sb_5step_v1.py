"""ICT Silver Bullet 5-step walkthrough (CSID candidate)
Source: YT ks6wOGghzoA (ICT FINATIC 2026 walkthrough) + airKam uaz-c6YLAF0 Edgeful proof.

5 steps:
  1. Bias census: count bullish vs bearish displacement of last N swings on
     current TF + a synthesized HTF (5x aggregation = 25m if 5m bars, 75m if 15m).
  2. Liquidity sweep: prior swing high/low swept by 1+ bar in last `sweep_window` bars.
  3. MSS: body candle close back inside (opposite-direction close after sweep).
  4. FVG formed since MSS, with min size in ATR units.
  5. Entry at FVG midpoint inside macro window (default 13-16 UTC = NY AM).

Optional breaker/mitigation filter: require an OB (last opposing candle before
the displacement) to bracket the FVG.
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
    macro_end = int(params.get("macro_end_utc_hour", 16))
    if not (macro_start <= hr < macro_end):
        return None

    swing_len = int(params.get("swing_len", 3))
    sweep_window = int(params.get("sweep_window", 8))
    bias_lookback = int(params.get("bias_lookback", 40))
    bias_threshold = float(params.get("bias_threshold", 0.55))
    fvg_min_atr = float(params.get("fvg_min_atr", 0.05))
    require_breaker = bool(params.get("require_breaker", True))
    max_age_bars = int(params.get("max_age_bars", 24))
    target_r = float(params.get("target_r", 2.0))
    stop_buf_atr = float(params.get("stop_buf_atr", 0.10))
    atr_period = int(params.get("atr_period", 14))

    h = bars["high"].values.astype(float)
    l = bars["low"].values.astype(float)
    c = bars["close"].values.astype(float)
    o = bars["open"].values.astype(float)

    # ATR
    prev_c = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr_series = pd.Series(tr).rolling(atr_period, min_periods=atr_period).mean().values
    atr = atr_series[cur_i]
    if not np.isfinite(atr) or atr <= 0:
        return None
    min_gap = atr * fvg_min_atr

    def find_swing_highs(start, end, length):
        out = []
        for i in range(start + length, end):
            if all(h[i] > h[i - j] for j in range(1, length + 1)) and all(h[i] >= h[i + j] for j in range(1, length + 1)):
                out.append(i)
        return out

    def find_swing_lows(start, end, length):
        out = []
        for i in range(start + length, end):
            if all(l[i] < l[i - j] for j in range(1, length + 1)) and all(l[i] <= l[i + j] for j in range(1, length + 1)):
                out.append(i)
        return out

    # Bias census: count HH/HL vs LH/LL over bias_lookback bars (approximation via swings)
    start_i = max(2, cur_i - bias_lookback)
    sh = find_swing_highs(start_i, cur_i + 1, swing_len)
    sl = find_swing_lows(start_i, cur_i + 1, swing_len)
    bull_votes = 0
    total_votes = 0
    if len(sh) >= 2:
        for i in range(1, len(sh)):
            total_votes += 1
            if sh[i] > sh[i - 1]:
                bull_votes += 1
    if len(sl) >= 2:
        for i in range(1, len(sl)):
            total_votes += 1
            if sl[i] > sl[i - 1]:
                bull_votes += 1
    if total_votes < 6:
        return None
    bull_bias_pct = bull_votes / total_votes
    bullish_bias = bull_bias_pct >= bias_threshold
    bearish_bias = bull_bias_pct <= (1.0 - bias_threshold)

    # Scan recent bars for sweep + MSS + FVG pattern
    scan_end = cur_i
    scan_start = max(swing_len + 2, cur_i - max_age_bars - sweep_window - 3)
    candidate = None
    candidate_dir = None
    candidate_swing_idx = None

    # Look for BSL sweep (short) and SSL sweep (long)
    for j in range(scan_start + sweep_window, scan_end):
        # detect swing high pivot at j (after j-swing_len..j, but we already computed sh)
        # simpler: take any recent swing high and check if swept since
        pass

    # More efficient: iterate recent bars, check sweep+MSS+FVG sequence
    for j in range(scan_start + 2, scan_end):
        # BSL sweep candidate: high above recent high then close back inside
        prior_window_start = max(0, j - 24)
        prior_high = float(np.max(h[prior_window_start:j]))
        prior_low = float(np.min(l[prior_window_start:j]))
        # Sweep of buy side: bar j wicks above prior_high, body closes below
        if h[j] > prior_high and c[j] <= prior_high and o[j] > prior_high * 0.999:
            if bearish_bias:
                # Look for FVG in next bars (bearish FVG: i-2.high < i.low for i>j)
                for i in range(j + 1, min(cur_i + 1, j + 1 + max_age_bars)):
                    if i - 2 < 0:
                        continue
                    if h[i - 2] < l[i] and (l[i] - h[i - 2]) >= min_gap:
                        # Bearish FVG found at [i-2,i]
                        top = l[i]
                        bot = h[i - 2]
                        # Optional breaker check: OB between bot and top is OK if candle i-1
                        # was a down candle that engulfed an up candle before i-2.
                        if require_breaker:
                            if i - 1 < 1:
                                continue
                            if not (c[i - 1] < c[i - 2]):
                                continue
                        # Check current bar retraces into FVG
                        if h[cur_i] >= bot and c[cur_i] <= top:
                            mid = (top + bot) / 2.0
                            stop = top + atr * stop_buf_atr
                            risk = stop - mid
                            if risk > 0:
                                target = mid - risk * target_r
                                candidate = {"direction": "short", "entry": mid, "stop": stop, "target": target}
                                candidate_dir = "short"
                                break
                if candidate is not None:
                    break

        # SSL sweep candidate: bar j wicks below prior_low, body closes above
        if l[j] < prior_low and c[j] >= prior_low and o[j] < prior_low * 1.001:
            if bullish_bias:
                for i in range(j + 1, min(cur_i + 1, j + 1 + max_age_bars)):
                    if i - 2 < 0:
                        continue
                    if l[i - 2] > h[i] and (l[i - 2] - h[i]) >= min_gap:
                        # Bullish FVG found
                        top = l[i - 2]
                        bot = h[i]
                        if require_breaker:
                            if i - 1 < 1:
                                continue
                            if not (c[i - 1] > c[i - 2]):
                                continue
                        if l[cur_i] <= top and c[cur_i] >= bot:
                            mid = (top + bot) / 2.0
                            stop = bot - atr * stop_buf_atr
                            risk = mid - stop
                            if risk > 0:
                                target = mid + risk * target_r
                                candidate = {"direction": "long", "entry": mid, "stop": stop, "target": target}
                                candidate_dir = "long"
                                break
                if candidate is not None:
                    break

    return candidate