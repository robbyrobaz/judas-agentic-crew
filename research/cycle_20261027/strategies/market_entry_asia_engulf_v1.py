"""ASIA-SESSION SWEEP + ENGULFING REJECTION, market entry (2026-10-27 cycle).

SOURCE
------
YT:f6RlUvPdKzs ("Gold Price Tomorrow? XAUUSD SMC ICT Liquidity Levels To
Watch", Aarav Kumar FX, 2026-10-26).  The mechanical rules he states:

  1. "if Gold is to provide a good trade or a good setup, it can only happen
     by sweeping the Asia session"  -> the trigger is a raid of the Asian
     session range, not an arbitrary level.
  2. "There should be some POI from where I feel the market will fall.  I
     am not finding that on the higher time frame.  So I cannot take this
     trade at all."  -> an HTF POI must exist in the trade direction,
     otherwise the setup is invalid.
  3. "If you look at the 15-minute chart, the candle did not close here ...
     the next candle that forms is a bearish candle ... it engulfed, right?
     This became a bearish engulfing.  Because this candle has more depth
     than the previous candle ... you could have taken a simple trade at
     the CANDLE'S CLOSE, and your SL would have been a bit large ... you
     would keep your SL above the body ... and you would simply target this
     swing low."
     -> entry at the close of the engulfing candle, stop above the
     engulfing body, target the opposite swing low.
  4. "I do not trade against the bias."

Everything here is a CLOSE entry, so the house stamp, the fair book and the
live book (market at the next bar's open) agree -- no retro-fill premium
(findings #9145/#9146).  The structural levels (Asia range, engulfing body)
are used only to define the STOP and the zone tests.

Stateless; numpy + pandas only (sandbox-safe).
"""
import numpy as np
import pandas as pd


def evaluate(bars, params):
    if bars is None or len(bars) < 300:
        return None
    n = len(bars)
    last = n - 1

    asia_start = int(params.get("asia_start_utc", 18))
    asia_end = int(params.get("asia_end_utc", 2))
    sweep_window = int(params.get("sweep_window", 12))
    engulf_window = int(params.get("engulf_window", 6))
    sweep_buf = float(params.get("sweep_buffer", 0.05))
    rr = float(params.get("rr", 3.0))
    atr_period = int(params.get("atr_period", 14))
    require_poi = bool(params.get("require_poi", True))
    poi_lookback = int(params.get("poi_lookback", 120))
    require_bias = bool(params.get("require_bias", False))
    ema_period = int(params.get("ema_period", 0))
    target_mode = str(params.get("target_mode", "rr"))
    max_bars_since_asia = int(params.get("max_bars_since_asia", 40))

    h_v = bars["high"].to_numpy(float)
    l_v = bars["low"].to_numpy(float)
    o_v = bars["open"].to_numpy(float)
    c_v = bars["close"].to_numpy(float)

    try:
        ts_np = bars["ts"].to_numpy()
        hours = (ts_np.astype("datetime64[h]").astype("int64"))
    except Exception:
        return None
    cur_h = int(hours[last])

    rngs = h_v[last - atr_period + 1:last + 1] - l_v[last - atr_period + 1:last + 1]
    atr = float(np.mean(rngs)) if len(rngs) else 0.0
    if atr <= 1e-9:
        return None

    # ------------------------------------------------- the Asian session range
    # the most recent completed Asian session (18:00 -> 02:00 UTC by default)
    found = None
    for back in range(0, 4):
        D = (cur_h // 24) - back
        start_h = D * 24 + asia_start
        end_h = D * 24 + (24 + asia_end if asia_end <= asia_start else asia_end)
        if start_h > cur_h:
            continue
        idx = np.where((hours >= start_h) & (hours < end_h))[0]
        if len(idx) < 4:
            continue
        found = (float(np.max(h_v[idx])), float(np.min(l_v[idx])),
                 int(idx[0]), int(idx[-1]))
        break
    if found is None:
        return None
    a_hi, a_lo, a_start, a_end = found
    if a_hi <= a_lo:
        return None
    if last - a_end > max_bars_since_asia:
        return None

    # ------------------------------------------------------------- the sweep
    sweep_dir = None
    sweep_bar = None
    lo = max(a_end, last - sweep_window)
    for j in range(lo, last + 1):
        if h_v[j] > a_hi + sweep_buf * atr and c_v[j] < a_hi:
            sweep_dir, sweep_bar = "short", j
            break
        if l_v[j] < a_lo - sweep_buf * atr and c_v[j] > a_lo:
            sweep_dir, sweep_bar = "long", j
            break
    if sweep_dir is None or sweep_bar is None:
        return None

    # ------------------------------------------------------- engulfing candle
    eng_bar = None
    hi = min(last, sweep_bar + engulf_window)
    for j in range(max(sweep_bar + 1, 1), hi + 1):
        prev_bull = c_v[j - 1] > o_v[j - 1]
        prev_bear = c_v[j - 1] < o_v[j - 1]
        cur_bull = c_v[j] > o_v[j]
        cur_bear = c_v[j] < o_v[j]
        if sweep_dir == "short" and prev_bull and cur_bear and \
                o_v[j] >= c_v[j - 1] and c_v[j] <= o_v[j - 1] and \
                (o_v[j] - c_v[j]) > (c_v[j - 1] - o_v[j - 1]):
            eng_bar = j
            break
        if sweep_dir == "long" and prev_bear and cur_bull and \
                o_v[j] <= c_v[j - 1] and c_v[j] >= o_v[j - 1] and \
                (c_v[j] - o_v[j]) > (o_v[j - 1] - c_v[j - 1]):
            eng_bar = j
            break
    if eng_bar is None:
        return None

    entry = float(c_v[last])          # HONEST: close of the decision bar
    if sweep_dir == "short":
        stop = float(max(h_v[eng_bar], o_v[eng_bar])) + 0.15 * atr
        # "keep your SL above the body": body top of the engulfing candle
        stop = max(stop, float(max(o_v[eng_bar], c_v[eng_bar])) + 0.05 * atr)
        risk = entry - stop
        if risk <= 0:
            return None
        target = entry - rr * risk
        if target_mode == "swing":
            sw_low = float(np.min(l_v[sweep_bar:last + 1]))
            target = sw_low
        if target >= entry:
            target = entry - rr * risk
    else:
        stop = float(min(l_v[eng_bar], o_v[eng_bar])) - 0.15 * atr
        stop = min(stop, float(min(o_v[eng_bar], c_v[eng_bar])) - 0.05 * atr)
        risk = stop - entry
        if risk <= 0:
            return None
        target = entry + rr * risk
        if target_mode == "swing":
            sw_high = float(np.max(h_v[sweep_bar:last + 1]))
            target = sw_high
        if target <= entry:
            target = entry + rr * risk

    # ----------------------------------------------------- HTF POI (rule 2)
    if require_poi:
        pl = max(0, last - poi_lookback)
        hi_arr = h_v[pl:last + 1]
        lo_arr = l_v[pl:last + 1]
        # suffix min/max of the window (price never trading back into a gap)
        smin = np.minimum.accumulate(lo_arr[::-1])[::-1]
        smax = np.maximum.accumulate(hi_arr[::-1])[::-1]
        poi = False
        m = len(hi_arr)
        for j in range(0, max(0, m - 2)):
            j2 = j + 2
            if sweep_dir == "long" and lo_arr[j2] > hi_arr[j]:
                # bullish FVG -> a POI ABOVE; unfilled if price never traded
                # back down into it
                if hi_arr[j] >= entry and smin[j2 + 1] > lo_arr[j2]:
                    poi = True
                    break
            if sweep_dir == "short" and hi_arr[j2] < lo_arr[j]:
                # bearish FVG -> a POI BELOW
                if lo_arr[j] <= entry and smax[j2 + 1] < hi_arr[j2]:
                    poi = True
                    break
        if not poi:
            return None

    if require_bias and ema_period > 0:
        alpha = 2.0 / (ema_period + 1.0)
        ema = float(c_v[0])
        for x in c_v:
            ema += alpha * (float(x) - ema)
        if sweep_dir == "long" and entry < ema:
            return None
        if sweep_dir == "short" and entry > ema:
            return None

    return {"ts": bars["ts"].iloc[last], "direction": sweep_dir,
            "entry": entry, "stop": float(stop), "target": float(target)}
