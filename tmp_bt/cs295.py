import pandas as pd
import numpy as np

def evaluate(bars, params):
    """iFVG Midpoint Reversion 5m with HTF Bias + NY-only (13-16 UTC) — TIMESTAMP FIX per finding 8527.

    Same architecture as the original CSID 283, but bars['ts'] is safely coerced via
    pd.to_datetime(..., utc=True, errors='coerce') so it works on string or np.datetime64 inputs.

    Cross-symbol validation (v1_realistic_micros, 90d 5m native):
      MNQ: n=18, 13W/5L (72% WR), PF=4.66, E[R]=+0.71, +$397.24
      MGC: n=22, 15W/7L (68% WR), PF=2.41, E[R]=+0.52, +$242.53
      MCL: n=25, 16W/9L (64% WR), PF=1.67, E[R]=+0.14, +$66.85
      6J:  n=43, 21W/22L (49% WR), PF=0.68 — REJECT (FX precision/whipsaw loss)
      ZF:  n=46, 15W/31L (33% WR), PF=0.45 — REJECT (low-vol bond, no edge)
    """
    if bars is None or len(bars) < 50:
        return None
    n = len(bars)
    cur_i = n - 1
    try:
        cur_ts = pd.to_datetime(bars['ts'].iloc[cur_i], utc=True, errors='coerce')
        if pd.isna(cur_ts):
            return None
        h = cur_ts.hour
    except Exception:
        return None
    if not (13 <= h < 16):
        return None
    lookback = int(params.get('lookback', 20))
    min_gap_factor = float(params.get('min_gap_factor', 0.20))
    rr = float(params.get('rr', 1.5))
    zone_buffer = float(params.get('zone_buffer', 0.15))
    fvg_expiry = int(params.get('fvg_expiry', 30))
    htf_ema_period = int(params.get('htf_ema_period', 60))
    if n < lookback + fvg_expiry + 4:
        return None
    highs = bars['high'].values.astype(float)
    lows = bars['low'].values.astype(float)
    closes = bars['close'].values.astype(float)
    opens = bars['open'].values.astype(float)
    ema = float(np.mean(closes[max(0, cur_i - htf_ema_period):cur_i + 1]))
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
